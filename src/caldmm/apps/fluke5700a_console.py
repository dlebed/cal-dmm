"""Fluke 5700A / 5720A service and manual console.

A line-oriented operator console for **triage, manual sourcing and escape-hatch
access**. It is the first tool used on a calibrator that has just arrived: run
diagnostics, read and group faults, step through a halted diagnostic
fault-by-fault, and source arbitrary values while probing the analog boards.

Two modes:

* **one-shot** — ``--out``/``--ohms``/``--status``/``--faults``/``--diag``
  execute and exit, for scripting and quick checks. A step that is refused —
  an out-of-envelope value, a declined confirmation, the live-output
  interlock — **abandons the remaining actions and exits 1**, so a script can
  never energize on the back of a source that did not happen;
* **REPL** — the default, where triage happens.

Everything the console does goes through :class:`caldmm.drivers.fluke5700a`.
This module adds three things the driver deliberately leaves out:

1. **an operator-facing value syntax** (``100mV``, ``1kHz``, ``1.9M``) that is
   converted to plain base units *before* the driver is called — the driver
   must never see a multiplier prefix (guide §3.6);
2. **the safety choreography** — explicit confirmation before energizing
   >= :data:`HIGH_VOLTAGE_V`, a live-output interlock before every long-term
   command, and a SIGINT that is *deferred* while a long-term command runs
   because there is no remote abort;
3. **formatters** — every status register decoded by bit name, and faults
   grouped by the assembly named in their text, which is the view that
   isolates a bad board.

Value syntax (see :func:`parse_value`)
--------------------------------------

Multiplier prefixes are **strict SI and case-sensitive**: ``m`` is milli and
``M`` is mega, everywhere, in every function. That is deliberately *not* the
calibrator's own syntax, where ``MA`` alone means milliamps but ``MAOHM`` and
``MAHZ`` mean mega (guide §3.6) — a rule that cannot be applied consistently
and is a poor thing to have to remember with 1100 V on the terminals. Because
1 MV and 1 MA are outside every 5700A range, a capital ``M`` on a volt or amp
amplitude can only ever be a typo for milli, so it is **rejected** rather than
interpreted; ``M`` on ohms and hertz (``1.9M``, ``1MHz``) means mega and is
accepted. Unit letters themselves are case-insensitive: ``mv``, ``mV`` and
``MV`` are milli, milli, and an error.
"""

import argparse
import contextlib
import re
import signal
import sys
import time

from dataclasses import dataclass, field
from typing import Optional

from caldmm.drivers import fluke5700a as f57
from caldmm.drivers.fluke5700a import (
    CalibratorFault,
    FAULT_CODES,
    Fluke5700A,
    Fluke5700AError,
    ISR_OPER,
    STATE_NAMES,
    decode_esr,
    decode_isr,
    decode_stb,
)
from caldmm.cal.calreport import NO_ASSEMBLY, fault_assemblies
from caldmm.lib.console import OperatorConsole
from caldmm.lib.formatting import si_format
from caldmm.lib.signals import setup_graceful_shutdown


# ──────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────

#: Guide §8.6 / §19.2: the threshold above which the calibrator is dangerous
#: and the one the guide uses for both the OPER inhibit and its "do not start a
#: long-term command with this live" recommendation.
HIGH_VOLTAGE_V = 22.0

#: The guide has no current analogue to the 22 V rule, so this is ours: at or
#: above 1 A a sustained output into a wiring fault or a shorted DUT is a
#: burn/fire hazard (the 5700A alone sources up to 2.2 A), and a long-term
#: command cannot be aborted remotely once started. Below it, prompting for
#: every microamp-to-milliamp point would train the operator to type ``yes``
#: without reading.
HIGH_CURRENT_A = 1.0

#: Guide §11.6: dc zeros are specified every 30 days; past that the calibrator
#: logs fault 1303 and its specifications no longer apply.
ZERO_CAL_MAX_DAYS = 30

#: Default poll interval for ``watch``.
WATCH_INTERVAL_SEC = 2.0

#: Command heads ``raw`` refuses without ``--service``. Queries are never
#: gated. These either write nonvolatile calibration memory, energize the
#: output with none of this console's confirmation, or are the service-only
#: commands the driver gates for itself. ``raw`` checks the **first token
#: only** — it is the documented escape hatch and bypasses all validation, so
#: a compound command can still get anything through.
RAW_SERVICE_ONLY = frozenset({
    "OPER",
    "CAL_ADJ", "CAL_CHK", "CAL_REF", "CAL_RNG", "CAL_STORE", "CAL_ZERO",
    "CAL_WBFLAT", "CAL_WBGAIN",
    "*PUD", "ETIME", "FATALCLR", "FATLCLR", "FORMAT",
})


class ConsoleError(Exception):
    """Operator input the console understood well enough to reject.

    Raised by the parsers and command handlers; the dispatcher prints it as a
    one-line message and keeps the session alive. Never a traceback.
    """


# ──────────────────────────────────────────────────────────────────────────
# Value parsing — pure, and the highest-value unit under test
# ──────────────────────────────────────────────────────────────────────────

#: Case-sensitive on purpose: ``m`` is milli and ``M`` is mega. ``K`` is
#: accepted alongside ``k`` because no SI prefix uses a capital K, so there is
#: nothing for it to collide with.
SI_PREFIXES = {
    "p": 1e-12,
    "n": 1e-9,
    "u": 1e-6,
    "µ": 1e-6,          # MICRO SIGN
    "μ": 1e-6,          # GREEK SMALL LETTER MU
    "m": 1e-3,
    "k": 1e3,
    "K": 1e3,
    "M": 1e6,
    "G": 1e9,
}

#: Unit spellings the console accepts, longest first so ``OHMS`` is matched
#: before ``OHM``. Matched case-insensitively; the value is the base unit the
#: driver is given.
UNIT_SPELLINGS = (
    ("OHMS", "OHM"),
    ("OHM", "OHM"),
    ("Ω", "OHM"),       # GREEK CAPITAL LETTER OMEGA
    ("Ω", "OHM"),       # OHM SIGN
    ("HZ", "HZ"),
    ("V", "V"),
    ("A", "A"),
)

_NUMBER_RE = re.compile(
    r"^\s*([+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?)\s*(.*?)\s*$")

#: Rendered next to the operator's mistake, so the rule is never more than one
#: error message away.
PREFIX_RULE = ("prefixes are case-sensitive SI: m=milli, M=mega "
               "(u/µ=micro, k=kilo, G=giga)")


def parse_value(text: str) -> "tuple[float, str]":
    """Parse ``"100mV"`` into ``(0.1, "V")`` — value in **base units**.

    Returns ``(value, unit)`` where *unit* is ``"V"``, ``"A"``, ``"OHM"``,
    ``"HZ"`` or ``""`` when the operator gave no unit (``10k``, ``2.5``).

    The multiplier prefix is case-sensitive strict SI — ``m`` milli, ``M``
    mega — and the unit letters are case-insensitive. A capital ``M`` on a
    volt or amp amplitude is **rejected**: the 5700A's own syntax would read
    it as milli (guide §3.6) while SI reads it as mega, and since no 5700A
    range reaches a megavolt or a megaamp it can only be a typo. Refusing is
    the only answer that cannot silently source the wrong thing.

    Raises :class:`ConsoleError` — never a traceback — on anything else.
    """
    if text is None:
        raise ConsoleError("expected a value, got nothing")
    raw = str(text).strip()
    if not raw:
        raise ConsoleError("expected a value, got an empty string")
    match = _NUMBER_RE.match(raw)
    if not match:
        raise ConsoleError(
            f"{raw!r} does not start with a number (expected something like "
            f"10V, 100mV, 1.5mA, 10k, 1kHz)")
    number, suffix = match.group(1), match.group(2)
    try:
        value = float(number)
    except ValueError:                       # pragma: no cover - regex guards
        raise ConsoleError(f"{number!r} is not a number") from None

    unit = ""
    for spelling, base_unit in UNIT_SPELLINGS:
        if suffix.upper().endswith(spelling):
            unit = base_unit
            suffix = suffix[:len(suffix) - len(spelling)]
            break

    prefix = suffix
    if not prefix:
        return value, unit
    if len(prefix) > 1:
        raise ConsoleError(
            f"unknown suffix {prefix!r} in {raw!r}: {PREFIX_RULE}, and the "
            f"unit is V, A, OHM or HZ")
    if prefix not in SI_PREFIXES:
        swapped = prefix.swapcase()
        hint = (f" (did you mean {swapped!r}?)" if swapped in SI_PREFIXES
                else "")
        raise ConsoleError(
            f"unknown multiplier {prefix!r} in {raw!r}{hint}: {PREFIX_RULE}")
    if prefix == "M" and unit in ("V", "A"):
        plural = {"V": "megavolts", "A": "megaamps"}[unit]
        raise ConsoleError(
            f"{raw!r} is ambiguous and is refused: in this console "
            f"{PREFIX_RULE}, so {raw!r} would mean {value:g} {plural} — "
            f"outside every 5700A range. The calibrator's own syntax reads a "
            f"bare M as milli (guide §3.6). Write {number}m{unit} for milli "
            f"or {number}{unit} for the base unit.")
    return value * SI_PREFIXES[prefix], unit


def parse_amplitude(text: str) -> "tuple[float, str]":
    """Parse an amplitude with a mandatory ``V``/``A``/``OHM`` unit."""
    value, unit = parse_value(text)
    if unit == "":
        raise ConsoleError(
            f"{text!r} has no unit: an amplitude needs V, A or OHM "
            f"(e.g. 10V, 1.5mA, 10kohm). Use the 'ohms' command for a bare "
            f"resistance.")
    if unit == "HZ":
        raise ConsoleError(
            f"{text!r} is a frequency, not an amplitude; the frequency is the "
            f"second argument: out 100V 1kHz")
    return value, unit


def parse_frequency(text: str) -> float:
    """Parse a frequency; a bare number is hertz."""
    value, unit = parse_value(text)
    if unit not in ("", "HZ"):
        raise ConsoleError(
            f"{text!r} is not a frequency (its unit is {unit}); write it as "
            f"1kHz, 1MHz or a plain number of hertz")
    return value


def parse_ohms(text: str) -> float:
    """Parse a resistance; a bare number (or ``10k``, ``1.9M``) is ohms."""
    value, unit = parse_value(text)
    if unit not in ("", "OHM"):
        raise ConsoleError(
            f"{text!r} is not a resistance (its unit is {unit}); write it as "
            f"10k, 1.9M, 100 or 10kohm")
    return value


def parse_onoff(text: str) -> bool:
    """Parse ``on``/``off`` (and the usual synonyms) into a bool."""
    token = str(text).strip().lower()
    if token in ("on", "1", "true", "yes", "y"):
        return True
    if token in ("off", "0", "false", "no", "n"):
        return False
    raise ConsoleError(f"expected 'on' or 'off', got {text!r}")


_BARE_NUMBER_RE = re.compile(r"^[+-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?$")


def glue_value_tokens(tokens) -> "list[str]":
    """Join ``["10", "V"]`` into ``["10V"]`` so spaces are forgiven.

    Only a bare number immediately followed by a pure unit/prefix token is
    joined, so ``out 100 V 1 kHz`` and ``out 100V 1kHz`` are the same command
    while ``calshift CAL DC220V`` is left alone.
    """
    out: "list[str]" = []
    index = 0
    items = list(tokens)
    while index < len(items):
        token = items[index]
        following = items[index + 1] if index + 1 < len(items) else None
        if (following is not None and _BARE_NUMBER_RE.match(token)
                and not _BARE_NUMBER_RE.match(following)
                and _is_unit_token(following)):
            out.append(token + following)
            index += 2
            continue
        out.append(token)
        index += 1
    return out


def _is_unit_token(token: str) -> bool:
    """True when *token* is only a multiplier and/or a unit, no digits."""
    if not token or any(char.isdigit() for char in token):
        return False
    try:
        parse_value("1" + token)
    except ConsoleError:
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────
# Formatters — pure functions, so every screen is testable with no instrument
# ──────────────────────────────────────────────────────────────────────────

_UNIT_SYMBOLS = {"V": "V", "A": "A", "OHM": "Ω", "HZ": "Hz",
                 "DBM": "dBm", "PPM": " ppm", "PCT": " %"}


def format_amplitude(value: Optional[float], unit: str = "") -> str:
    """Render a value with an SI prefix and the unit's symbol."""
    if value is None:
        return "(none)"
    symbol = _UNIT_SYMBOLS.get(str(unit).upper(), str(unit))
    return si_format(float(value), 6, symbol)


def format_output(amplitude: Optional[float], unit: str,
                  frequency: float = 0.0) -> str:
    """Render a setpoint the way ``OUT?`` reports one: value, unit, frequency.

    ``100 V @ 1 kHz`` for ac, ``100 V dc`` for dc, and no suffix at all in the
    ohms function, where a frequency is fault 812.
    """
    text = format_amplitude(amplitude, unit)
    if frequency:
        return f"{text} @ {format_amplitude(frequency, 'HZ')}"
    if str(unit).upper() in ("V", "A"):
        return f"{text} dc"
    return text


def format_register(name: str, value: Optional[int], decoded) -> "list[str]":
    """Render one status register with **every** bit named, set bits first."""
    if value is None:
        return [f"{name:<5} (unavailable)"]
    set_bits = [bit for bit, on in decoded.items() if on]
    clear_bits = [bit for bit, on in decoded.items() if not on]
    width = 4 if name == "ISR" else 2
    return [
        f"{name:<5} 0x{value:0{width}X}  set: "
        f"{', '.join(set_bits) if set_bits else '(none)'}",
        f"{'':<5} {'':{width + 2}}  clear: {', '.join(clear_bits)}",
    ]


def format_isr(value: int) -> str:
    """``ISR?`` decoded by bit name (non-destructive read, guide §8.4)."""
    return "\n".join(format_register("ISR", value, decode_isr(value)))


def format_esr(value: int) -> str:
    """``*ESR?`` decoded by bit name. **Reading the ESR clears it** (§8.3)."""
    return "\n".join(format_register("ESR", value, decode_esr(value)))


def format_stb(value: int) -> str:
    """A status byte decoded by bit name (guide §8.2)."""
    return "\n".join(format_register("STB", value, decode_stb(value)))


def group_faults_by_assembly(faults) -> "dict[str, list]":
    """Group ``(code, text)`` faults by every assembly their text names.

    A fault naming two assemblies (``A14/A15``) appears under **both**: the
    question this view answers is "which board is implicated in everything I
    am seeing", and a shared-blame fault is evidence for each of its boards.
    Groups are ordered by size, largest first, so the most-implicated assembly
    leads; faults naming no assembly land in :data:`NO_ASSEMBLY`, always last.
    """
    groups: "dict[str, list]" = {}
    for code, text in faults:
        keys = fault_assemblies(text) or (NO_ASSEMBLY,)
        for key in keys:
            groups.setdefault(key, []).append((code, text))
    ordered = sorted(
        (key for key in groups if key != NO_ASSEMBLY),
        key=lambda key: (-len(groups[key]), key))
    result = {key: groups[key] for key in ordered}
    if NO_ASSEMBLY in groups:
        result[NO_ASSEMBLY] = groups[NO_ASSEMBLY]
    return result


def format_fault(code: int, text: str) -> str:
    """One fault as ``code  text  (assembly)``."""
    assemblies = fault_assemblies(text)
    suffix = f"  ({'/'.join(assemblies)})" if assemblies else ""
    return f"{code:>5}  {text}{suffix}"


def format_faults(faults) -> str:
    """The drained fault queue, in queue order (earliest first, §8.5)."""
    if not faults:
        return "  fault queue empty"
    lines = [f"  {len(faults)} fault(s), earliest first "
             f"(the earliest points at the cause — guide §8.5):"]
    lines += [f"  {format_fault(code, text)}" for code, text in faults]
    return "\n".join(lines)


def format_fault_groups(faults) -> str:
    """Faults grouped by assembly — the board-isolation view."""
    if not faults:
        return "  no faults to group"
    groups = group_faults_by_assembly(faults)
    lines = [f"  {len(faults)} fault(s) across {len(groups)} group(s); a "
             f"fault naming two assemblies appears under both:"]
    for assembly, entries in groups.items():
        lines.append(f"    {assembly}  — {len(entries)} fault(s)")
        for code, text in entries:
            lines.append(f"      {format_fault(code, text)}")
    return "\n".join(lines)


def _normalize(text: Optional[str]) -> str:
    return " ".join((text or "").split()).casefold()


def format_explain(code: int, offline: Optional[str],
                   instrument: Optional[str]) -> str:
    """Show the offline table and ``EXPLAIN?`` side by side.

    Both are printed always, and a disagreement is called out: the offline
    table is transcribed from [OM Appendix A] including its typos (guide §13
    item 31), and a sick unit's own ``EXPLAIN?`` may misbehave — so neither is
    authoritative on its own and the operator should see both.
    """
    lines = [f"  fault {code}"]
    lines.append(f"    offline table : "
                 f"{offline if offline else '(not in the offline table)'}")
    lines.append(f"    EXPLAIN?      : "
                 f"{instrument if instrument else '(no answer)'}")
    if offline and instrument and _normalize(offline) != _normalize(instrument):
        lines.append("    ! the two sources DISAGREE. The offline table is "
                     "transcribed verbatim from [OM Appendix A] (typos and "
                     "all, guide §13 item 31); a sick unit's EXPLAIN? "
                     "can also misbehave. Trust the numeric code, not either "
                     "text.")
    return "\n".join(lines)


@dataclass
class StatusSnapshot:
    """Everything ``status`` shows, read once so the formatter stays pure."""

    state: "Optional[tuple[int, str]]" = None
    isr: Optional[int] = None
    esr: Optional[int] = None
    stb: Optional[int] = None
    out: "Optional[tuple[float, str, float]]" = None
    range_id: Optional[str] = None
    uncert: "Optional[tuple[Optional[float], str, int]]" = None
    errors: "list[str]" = field(default_factory=list)


def format_status(snap: StatusSnapshot) -> str:
    """Render a :class:`StatusSnapshot` — one screen, no hex-only fields."""
    lines = ["── 5700A status "
             "──────────"]
    if snap.state is None:
        lines.append("  STATE (unavailable)")
    else:
        code, substate = snap.state
        name = STATE_NAMES.get(code, "unknown state")
        lines.append(f"  STATE {code}  {name}"
                     + (f"  — {substate}" if substate else ""))
    if snap.out is None:
        lines.append("  OUT   (unavailable)")
    else:
        lines.append(f"  OUT   {format_output(*snap.out)}")
    lines.append(f"  RANGE {snap.range_id or '(unavailable)'}")
    if snap.uncert is None:
        lines.append("  UNCERT (unavailable)")
    elif snap.uncert[0] is None:
        lines.append("  UNCERT no specification for the present output "
                     "(the -1.0 sentinel)")
    else:
        value, units, days = snap.uncert
        lines.append(f"  UNCERT {format_amplitude(value, units)} "
                     f"({days} day interval)")
    for name, value, decoder in (("ISR", snap.isr, decode_isr),
                                 ("ESR", snap.esr, decode_esr),
                                 ("STB", snap.stb, decode_stb)):
        decoded = decoder(value) if value is not None else {}
        lines += [f"  {line}"
                  for line in format_register(name, value, decoded)]
    if snap.isr is not None:
        live = "OPERATE — the terminals are LIVE" if snap.isr & ISR_OPER \
            else "STANDBY"
        lines.append(f"  OUTPUT {live}")
    lines.append("  (reading *ESR? clears it, and the serial poll clears "
                 "RQS — guide §8.2, §8.3)")
    for problem in snap.errors:
        lines.append(f"  ! {problem}")
    return "\n".join(lines)


def format_isr_change(previous: Optional[int], now: int) -> str:
    """``watch``'s change line: which bits went up and which went down."""
    if previous is None:
        decoded = decode_isr(now)
        names = [bit for bit, on in decoded.items() if on] or ["(none set)"]
        return f"ISR 0x{now:04X}  {' '.join(names)}"
    before, after = decode_isr(previous), decode_isr(now)
    changes = [f"{'+' if after[bit] else '-'}{bit}"
               for bit in after if after[bit] != before[bit]]
    return (f"ISR 0x{previous:04X} -> 0x{now:04X}  "
            f"{' '.join(changes) if changes else '(no bit changed)'}")


def format_banner(identity, options, ontime: Optional[int],
                  zero_days: Optional[int], cal_days: Optional[int],
                  cal_interval: Optional[int] = None) -> str:
    """The startup screen: who this is, how warm, how stale.

    Everything the operator sees afterwards is framed by this — a unit that
    has been on for 3 minutes and last had its zeros done 400 days ago is not
    one whose readings mean anything yet.
    """
    lines = ["── Fluke 5700A/5720A service console "
             "────────"]
    if identity is None:
        lines.append("  *IDN? (unavailable)")
    else:
        lines.append(f"  {identity.manufacturer} {identity.model}  "
                     f"s/n {identity.serial}  firmware {identity.firmware}")
        if identity.has_amplifier:
            lines.append("  auxiliary amplifier firmware reported "
                         "(third *IDN? field is not '*')")
    lines.append(f"  options: {', '.join(options) if options else '(none)'}")
    if ontime is None:
        lines.append("  ONTIME? (unavailable)")
    else:
        lines.append(f"  powered on for {ontime} min this session")
        if ontime < f57.WARMUP_MAX_MINUTES:
            lines.append(f"  ! WARM-UP: specifications need twice the time "
                         f"since last warmed up, up to "
                         f"{f57.WARMUP_MAX_MINUTES} min (guide §11.6). "
                         f"Readings taken now are indicative only.")
    lines.append(f"  days since dc zeros : "
                 f"{zero_days if zero_days is not None else '(unavailable)'}")
    if zero_days is not None and zero_days > ZERO_CAL_MAX_DAYS:
        lines.append(f"  ! DC ZEROS ARE {zero_days} DAYS OLD (limit "
                     f"{ZERO_CAL_MAX_DAYS}). The calibrator logs fault 1303 "
                     f"and its specifications do not apply until 'calzero' "
                     f"is run (guide §11.6).")
    interval = (f" of a {cal_interval} day interval"
                if cal_interval is not None else "")
    lines.append(f"  days since calibration: "
                 f"{cal_days if cal_days is not None else '(unavailable)'}"
                 f"{interval}")
    if (cal_days is not None and cal_interval is not None
            and cal_days > cal_interval):
        lines.append(f"  ! calibration is {cal_days - cal_interval} days past "
                     f"its interval; UNCERT? no longer describes this unit.")
    lines.append("  type 'help' for the command list, 'quit' to leave "
                 "(the output is dropped to standby on exit)")
    return "\n".join(lines)


def long_term_warning(what: str) -> "tuple[str, tuple]":
    """Banner + body for "about to start a long-term command"."""
    return (f"── {what}: long-term command ──", (
        "There is NO REMOTE ABORT. STBY is rejected with fault 2002 while a",
        "long-term command runs (guide §19.2 gap G6), so this console",
        "cannot stop it once started.",
        "",
        "The front-panel STBY key or line power is the only stop — keep "
        "one",
        "within reach. Ctrl-C is noted but will NOT interrupt the poll.",
    ))


def output_hazard(amplitude: float, unit: str) -> "Optional[str]":
    """Why this output demands explicit operator confirmation, or ``None``.

    Pure, so the thresholds can be tested exhaustively. The caller must
    resolve a dBm output to volts through ``VOUT?`` **before** asking — the
    dBm reference is 600 Ω on the standard ac output, so 42 dBm is ~97 V —
    and a unit still reading ``DBM`` here means that resolution failed, which
    is treated as hazardous rather than assumed safe.
    """
    resolved = unit.upper()
    if resolved == "V" and abs(amplitude) >= HIGH_VOLTAGE_V:
        return f"is at or above {HIGH_VOLTAGE_V:g} V"
    if resolved == "DBM":
        return ("is in dBm and VOUT? could not express it in volts, so it "
                "cannot be proven safe")
    if resolved == "A" and abs(amplitude) >= HIGH_CURRENT_A:
        return f"is at or above {HIGH_CURRENT_A:g} A"
    return None


def interlock_refusal(what: str, amplitude: float, unit: str,
                      frequency: float, reason: str) -> str:
    """The refusal message for a long-term command with a dangerous output.

    Carried by a :class:`ConsoleError`, so the dispatcher supplies the leading
    ``!`` marker and the continuation lines are indented to sit under it.
    """
    return (
        f"refusing to start {what}: the output is in OPERATE at "
        f"{format_output(amplitude, unit, frequency)}, which {reason}.\n"
        f"    A long-term command cannot be aborted remotely and STBY is "
        f"rejected with fault 2002 while it runs (guide §19.2), so it "
        f"must not be started with a dangerous output live.\n"
        f"    Type 'stby' to de-energize, then repeat {what}.")


#: ``(name, arguments, description)`` for every REPL command, in the order the
#: help screen shows them. Section headers are entries whose name is ``""``.
COMMAND_HELP = (
    ("", "", "Output and manual sourcing"),
    ("out", "<value><unit> [<freq>]", "source a value, staying in STANDBY "
                                      "(out 10V / out 100V 1kHz / out 1.5mA)"),
    ("ohms", "<value>", "source a resistance (ohms 10k / 1.9M / 0)"),
    ("oper", "", f"energize the output (confirmation required at "
                 f">= {HIGH_VOLTAGE_V:g} V)"),
    ("stby", "", "de-energize the output"),
    ("settle", "", "wait for ISR SETTLED and report how long it took"),
    ("outq", "", "OUT? — what is really being sourced"),
    ("adjq", "", "ADJOUT? — the Control Display value"),
    ("uncert", "", "UNCERT? — the calibrator's own spec for this output"),
    ("range", "", "RANGE? — the selected range identifier"),
    ("rangelck", "on|off", "lock/unlock the present range"),
    ("rcomp", "on|off", "two-wire ohms compensation (<= 19 kΩ only)"),
    ("extsense", "on|off", "remote (4-wire) voltage sensing"),
    ("extguard", "on|off", "external voltage guard"),
    ("wband", "[on|off]", "Option 03 wideband output (50 Ω, 300 µV-3.5 V, "
                          "10 Hz-30 MHz); bare = query"),
    ("incr", "<±step>[unit]", "nudge the output / enter error mode "
                              "(incr -10uV / incr 1Hz)"),
    ("newref", "", "take the adjusted output as the new reference"),
    ("mult", "<factor>", "multiply the reference magnitude (mult 1.9)"),
    ("limit", "[<+lim> <-lim>]", "entry limits — the calibrator's own bound "
                                 "on what it will source; bare = query"),

    ("", "", "Status and faults"),
    ("status", "", "one screen: STATE, OUT, RANGE, UNCERT, ISR/ESR/STB"),
    ("isr", "", "ISR? decoded by bit name (non-destructive)"),
    ("iscr", "", "ISCR? — latched ISR changes (READING IT CLEARS IT)"),
    ("esr", "", "*ESR? decoded by bit name (READING IT CLEARS IT)"),
    ("stb", "", "serial poll decoded by bit name (clears RQS)"),
    ("faults", "", "drain the fault queue and group it by assembly"),
    ("explain", "<code>", "decode a fault from the offline table AND "
                          "EXPLAIN?, showing both"),
    ("state", "", "STATE? with its decoded name"),
    ("watch", "[interval]", "poll ISR + faults, print only on change, "
                            "Ctrl-C to stop"),

    ("", "", "Diagnostics"),
    ("test", "", "*TST? self-test (blocks; the duration is undocumented, "
                 "so it is measured)"),
    ("diag", "[target]", "run a diagnostic (default D5700), then group the "
                         "faults"),
    ("diagstep", "[target]", "DIAGFLT HALT + step fault by fault — the "
                             "board-isolation workflow"),
    ("diagflt", "[mode]", "show or set what a diagnostic fault does "
                          "(NVRAM setting)"),
    ("fatality", "", "FATALITY? — the fatal-fault list (--service)"),
    ("fatalclr", "", "clear the fatal-fault list (--service)"),

    ("", "", "Calibration and housekeeping"),
    ("caldays", "[selector]", "CAL_DAYS? — days since CAL and ZERO"),
    ("caldate", "[selector]", "CAL_DATE? with the DATEFMT? field order"),
    ("calintv", "[days]", "CAL_INTV? — the calibration interval; with an "
                          "argument SETS it (NVRAM)"),
    ("calconf", "[95|99]", "CAL_CONF? — the confidence level; with an "
                           "argument SETS it (NVRAM)"),
    ("caltemp", "[celsius]", "CAL_TEMP? per activity; with an argument sets "
                             "it for the following cal commands"),
    ("calconst", "<name> [group]", "CAL_CONST? — one constant by name"),
    ("calshift", "<group> <range>", "CAL_SHIFT? — shifts for one range"),
    ("calslst", "[group]", "CAL_SLST? — shifts for every range"),
    ("calrpt", "[group]", "CAL_RPT? — the report, raw; 'calrpt summary "
                          "[group]' parses it"),
    ("calzero", "", "CAL_ZERO — dc zeros, ~2.5 min, long-term"),
    ("calchk", "", "CAL_CHK — calibration check, long-term"),
    ("calrng", "<id> <mag>|null|store", "range cal against an external "
                                        "standard (--service, guide §14.8)"),
    ("calwbgain", "pgain|ngain|store", "wideband gain cal (cable on SENSE; "
                                       "long-term)"),
    ("calwbflat", "start|null|store", "wideband flatness cal (INCR-null "
                                      "loop)"),
    ("calclst", "[group]", "CAL_CLST? — dump every calibration constant"),
    ("ohmsref", "[group]", "OHMSREF? — the internal resistance reference"),
    ("idn", "", "the parsed *IDN?"),
    ("opt", "", "the *OPT? option strings"),
    ("etime", "", "ETIME? — lifetime powered-on minutes"),
    ("ontime", "", "ONTIME? — minutes since power-up"),
    ("clock", "", "CLOCK? and DATEFMT?"),
    ("rptstr", "", "RPT_STR? — the user report string"),
    ("lrn", "", "*LRN? — the settings string (replaying it re-operates!)"),
    ("echo", "<text>", "ECHO? — the cheapest end-to-end link test"),

    ("", "", "Escape hatch and control"),
    ("raw", "<command...>", "send verbatim, BYPASSING ALL VALIDATION; a "
                            "trailing ? is read back"),
    ("local", "", "GTL — hand the front panel back to the operator"),
    ("help", "", "this screen"),
    ("quit", "", "standby, close the session and exit (also 'exit')"),
)


def format_help() -> str:
    """The ``help`` screen, including the value-syntax rules."""
    lines = ["── commands "
             "──────────"]
    for name, arguments, description in COMMAND_HELP:
        if not name:
            lines.append("")
            lines.append(f"  {description}")
            continue
        left = f"{name} {arguments}".strip()
        lines.append(f"    {left:<26} {description}")
    lines += [
        "",
        "  Value syntax",
        "    Multiplier prefixes are STRICT SI and CASE-SENSITIVE:",
        "      p n u/µ m   k/K M G      m = MILLI,  M = MEGA",
        "    Unit letters are case-insensitive: 10v, 10V and 10 V are equal.",
        "      out 100mV      100 millivolts       out 1.5mA   1.5 milliamps",
        "      out 100V 1kHz  100 V at 1 kHz       out 1V 1MHz 1 V at 1 MHz",
        "      ohms 10k       10 kΩ              ohms 1.9M   1.9 MΩ",
        "    The calibrator's OWN syntax differs: a bare M there means milli "
        "on",
        "    amplitudes but mega on ohms and hertz (guide §3.6). This "
        "console",
        "    does not copy that. Because no 5700A range reaches a megavolt "
        "or a",
        "    megaamp, 'out 100MV' can only be a typo for milli and is "
        "REFUSED",
        "    rather than guessed at. Everything is converted to plain base "
        "units",
        "    before it reaches the driver — the instrument never sees a "
        "prefix.",
        "",
        "  Safety",
        f"    Energizing >= {HIGH_VOLTAGE_V:g} V needs a typed 'yes'.",
        "    Long-term commands (test, diag, diagstep, calzero, calchk) are "
        "refused",
        f"    while an output of >= {HIGH_VOLTAGE_V:g} V is live, and CANNOT "
        f"BE ABORTED",
        "    remotely once started — the front-panel STBY key or line "
        "power is the",
        "    only stop (guide §19.2). Ctrl-C during one is noted, not "
        "obeyed.",
        "    'raw' bypasses every one of those checks.",
    ]
    return "\n".join(lines)


# ──────────────────────────────────────────────────────────────────────────
# Signal handling around long-running work
# ──────────────────────────────────────────────────────────────────────────

@contextlib.contextmanager
def deferred_sigint(what: str):
    """Note a Ctrl-C during *what* instead of acting on it.

    A long-term command has no remote abort, so tearing the session down on
    SIGINT would leave the calibrator running the command with nothing
    watching it. The handler records the interrupt and says so; the poll runs
    to completion and the caller reports it afterwards. The previous handler —
    normally the graceful-shutdown one — is restored on every exit path.

    Yields a one-key dict so the caller can test ``state["hit"]``.
    """
    state = {"hit": False}

    def handler(signum, frame):
        state["hit"] = True
        print(f"\n  Ctrl-C noted, but {what} cannot be aborted remotely "
              f"(guide §19.2). Waiting for it to finish; use the "
              f"front-panel STBY key if the output must come off.",
              flush=True)

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:                    # not the main thread (tests)
        yield state
        return
    try:
        yield state
    finally:
        signal.signal(signal.SIGINT, previous)


@contextlib.contextmanager
def prompt_sigint():
    """Make Ctrl-C at the REPL prompt cancel the line, not the session.

    :func:`caldmm.lib.signals.setup_graceful_shutdown` installs a handler
    that runs the cleanup and calls ``sys.exit`` — right for a stray Ctrl-C
    while something is running, wrong while merely waiting for the next
    command, where losing a triage session to a mistyped key is pure loss.
    This installs the interpreter's normal behaviour for the duration of the
    ``input()`` call only, and puts the graceful handler back afterwards.
    """

    def handler(signum, frame):
        raise KeyboardInterrupt

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:                    # not the main thread (tests)
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


@contextlib.contextmanager
def interruptible(state):
    """Let Ctrl-C stop a polling loop without ending the session."""

    def handler(signum, frame):
        state["stop"] = True

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:                    # not the main thread (tests)
        yield
        return
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous)


# ──────────────────────────────────────────────────────────────────────────
# The session
# ──────────────────────────────────────────────────────────────────────────

def read_status(dev) -> StatusSnapshot:
    """Read everything ``status`` shows, tolerating individual failures.

    A sick unit answers some queries and not others; one dead register must
    not cost the operator the whole screen, so each read is wrapped and its
    failure recorded on the snapshot.
    """
    snap = StatusSnapshot()
    for attribute, what, read in (
            ("state", "STATE?", lambda: dev.state()),
            ("out", "OUT?", lambda: dev.out_actual()),
            ("range_id", "RANGE?", lambda: dev.range_id()),
            ("uncert", "UNCERT?", lambda: dev.uncert()),
            ("isr", "ISR?", lambda: dev.query_int("ISR?")),
            ("esr", "*ESR?", lambda: dev.query_int("*ESR?")),
            ("stb", "serial poll", lambda: dev.serial_poll()),
    ):
        try:
            setattr(snap, attribute, read())
        except Exception as exc:
            snap.errors.append(f"{what} failed: {exc}")
    return snap


class ServiceConsole:
    """Command dispatch and the safety choreography around the driver."""

    def __init__(self, dev, console: Optional[OperatorConsole] = None, *,
                 service: bool = False):
        self.dev = dev
        self.console = console or OperatorConsole()
        self.service = service
        #: The exception the last :meth:`execute` swallowed, or ``None`` if it
        #: succeeded. The REPL ignores it — printing the message is enough
        #: there — but one-shot mode reads it to decide the exit status and
        #: whether to run the remaining actions at all. A refusal that a
        #: script cannot detect is a refusal that a script will override.
        self.last_error: Optional[BaseException] = None
        # Overridable so the tests do not sleep through a real poll interval.
        self.poll_interval = f57.LONG_TERM_POLL_INTERVAL_SEC
        self.watch_interval = WATCH_INTERVAL_SEC
        self._handlers = self._build_handlers()

    # ── plumbing ──────────────────────────────────────────────────────

    def out(self, *lines) -> None:
        for line in lines:
            print(line, flush=True)

    def _build_handlers(self) -> dict:
        handlers = {
            "out": self.cmd_out,
            "ohms": self.cmd_ohms,
            "oper": self.cmd_oper,
            "stby": self.cmd_stby,
            "settle": self.cmd_settle,
            "outq": self.cmd_outq,
            "adjq": self.cmd_adjq,
            "uncert": self.cmd_uncert,
            "range": self.cmd_range,
            "rangelck": self.cmd_rangelck,
            "rcomp": self.cmd_rcomp,
            "extsense": self.cmd_extsense,
            "extguard": self.cmd_extguard,
            "wband": self.cmd_wband,
            "incr": self.cmd_incr,
            "newref": self.cmd_newref,
            "mult": self.cmd_mult,
            "limit": self.cmd_limit,
            "status": self.cmd_status,
            "isr": self.cmd_isr,
            "iscr": self.cmd_iscr,
            "esr": self.cmd_esr,
            "stb": self.cmd_stb,
            "faults": self.cmd_faults,
            "explain": self.cmd_explain,
            "state": self.cmd_state,
            "watch": self.cmd_watch,
            "test": self.cmd_test,
            "diag": self.cmd_diag,
            "diagstep": self.cmd_diagstep,
            "diagflt": self.cmd_diagflt,
            "fatality": self.cmd_fatality,
            "fatalclr": self.cmd_fatalclr,
            "caldays": self.cmd_caldays,
            "caldate": self.cmd_caldate,
            "calintv": self.cmd_calintv,
            "calconf": self.cmd_calconf,
            "calconst": self.cmd_calconst,
            "calshift": self.cmd_calshift,
            "calslst": self.cmd_calslst,
            "calrpt": self.cmd_calrpt,
            "calzero": self.cmd_calzero,
            "calchk": self.cmd_calchk,
            "caltemp": self.cmd_caltemp,
            "calrng": self.cmd_calrng,
            "calwbgain": self.cmd_calwbgain,
            "calwbflat": self.cmd_calwbflat,
            "calclst": self.cmd_calclst,
            "ohmsref": self.cmd_ohmsref,
            "local": self.cmd_local,
            "idn": self.cmd_idn,
            "opt": self.cmd_opt,
            "etime": self.cmd_etime,
            "ontime": self.cmd_ontime,
            "clock": self.cmd_clock,
            "rptstr": self.cmd_rptstr,
            "lrn": self.cmd_lrn,
            "echo": self.cmd_echo,
            "raw": self.cmd_raw,
            "help": self.cmd_help,
        }
        return handlers

    def execute(self, line: str) -> bool:
        """Run one command line. Returns False when the session should end.

        Every foreseeable failure is caught and printed: a triage console that
        dies on a malformed argument, or on the very fault the operator is
        chasing, is worse than useless. **Each catch also records the
        exception on :attr:`last_error`** — swallowing a failure silently is
        fine for a human at a prompt, who has just read the message, and
        wrong for a script, which has not.
        """
        tokens = line.split()
        if not tokens or tokens[0].startswith("#"):
            return True
        name, arguments = tokens[0].lower(), tokens[1:]
        if name in ("quit", "exit", "q"):
            return False
        self.last_error = None
        handler = self._handlers.get(name)
        if handler is None:
            self.last_error = ConsoleError(f"unknown command {name!r}")
            self.out(f"  ! unknown command {name!r}. Type 'help' for the "
                     f"list.")
            return True
        try:
            handler(arguments)
        except ConsoleError as exc:
            self.last_error = exc
            self.out(f"  ! {exc}")
        except (CalibratorFault, Fluke5700AError) as exc:
            self.last_error = exc
            self.out(f"  ! {type(exc).__name__}: {exc}")
        except ValueError as exc:
            self.last_error = exc
            self.out(f"  ! refused: {exc}")
        except TimeoutError as exc:
            self.last_error = exc
            self.out(f"  ! timeout: {exc}")
        except KeyboardInterrupt as exc:
            self.last_error = exc
            self.out("  ! interrupted")
        except Exception as exc:                      # keep the session alive
            self.last_error = exc
            self.out(f"  ! unexpected {type(exc).__name__}: {exc}")
        return True

    @staticmethod
    def _need(arguments, count: int, usage: str) -> None:
        if len(arguments) < count:
            raise ConsoleError(f"usage: {usage}")

    def _report_faults(self, faults, *, grouped: bool = False) -> None:
        if grouped:
            self.out(format_fault_groups(faults))
        else:
            self.out(format_faults(faults))

    # ── output and manual sourcing ────────────────────────────────────

    def cmd_out(self, arguments) -> None:
        arguments = glue_value_tokens(arguments)
        self._need(arguments, 1, "out <value><unit> [<frequency>]")
        if len(arguments) > 2:
            raise ConsoleError(
                "usage: out <value><unit> [<frequency>] — amplitude and "
                "frequency travel together in one OUT (guide §7.3)")
        amplitude, unit = parse_amplitude(arguments[0])
        frequency = parse_frequency(arguments[1]) if len(arguments) > 1 else 0.0
        if unit == "OHM" and frequency:
            raise ConsoleError(
                "a frequency is never sent in the ohms function — that "
                "is fault 812 (guide §14.2)")
        self._source(amplitude, unit, frequency)

    def cmd_ohms(self, arguments) -> None:
        arguments = glue_value_tokens(arguments)
        self._need(arguments, 1, "ohms <value>  (ohms 10k / ohms 1.9M / "
                                 "ohms 0)")
        if len(arguments) > 1:
            # Same refusal as ``out``: silently dropping the extra token would
            # stage 10 kOhm for an operator who asked for something this
            # instrument cannot do, and let them find out at the terminals.
            raise ConsoleError(
                "usage: ohms <value> — a frequency is never sent in the ohms "
                "function, that is fault 812 (guide §14.2)")
        self._source(parse_ohms(arguments[0]), "OHM", 0.0)

    def _source(self, amplitude: float, unit: str, frequency: float) -> None:
        """Validate offline, then stage the value. Never energizes.

        Raises :class:`ConsoleError` rather than printing and returning, so
        that a caller — one-shot mode above all — can tell a refusal from a
        success. ``--out 2000V --operate`` must never go on to energize
        whatever was staged before it.
        """
        problems = f57.validate_output(amplitude, unit, frequency,
                                       options=getattr(self.dev, "options",
                                                       ()))
        if problems:
            listed = "\n".join(f"      {problem}" for problem in problems)
            raise ConsoleError(
                f"refusing to source "
                f"{format_output(amplitude, unit, frequency)}"
                f" — nothing was sent:\n{listed}")
        self.dev.set_output(amplitude, unit, frequency)
        self.out(f"  staged {format_output(amplitude, unit, frequency)}"
                 f"  (STANDBY — 'oper' to energize)")
        reason = output_hazard(amplitude, unit)
        if reason:
            self.out(f"  ! this output {reason}: 'oper' will ask "
                     f"you to type 'yes' before it energizes.")

    def _resolved_output(self) -> "tuple[float, str, float]":
        """``OUT?``, with a dBm output resolved to volts through ``VOUT?``.

        When ``VOUT?`` itself fails the dBm tuple is returned unresolved;
        :func:`output_hazard` then treats it as hazardous, which is the
        fail-safe direction.
        """
        amplitude, unit, frequency = self.dev.out_actual()
        if unit.upper() == "DBM":
            try:
                amplitude, unit, frequency = self.dev.vout_actual()
            except Exception:
                pass
        return amplitude, unit, frequency

    def cmd_oper(self, arguments) -> None:
        amplitude, unit, frequency = self._resolved_output()
        reason = output_hazard(amplitude, unit)
        if reason:
            setpoint = format_output(amplitude, unit, frequency)
            what = f"energizing {setpoint} on the output terminals"
            banner = ("HIGH CURRENT" if unit.upper() == "A"
                      else "HIGH VOLTAGE")
            self.console.show(
                f"── {banner} ──",
                f"About to energize {setpoint}, which {reason}.",
                "Check the leads, the DUT and everyone's hands first.",
                panel_msg=banner)
            if not self.console.confirm_explicit(what):
                raise ConsoleError(
                    "declined — OPER was not sent; the output is still in "
                    "standby.")
        self.dev.operate()
        amplitude, unit, frequency = self.dev.out_actual()
        self.out(f"  OPERATE — live at "
                 f"{format_output(amplitude, unit, frequency)}")

    def cmd_stby(self, arguments) -> None:
        self.dev.standby()
        self.out("  STANDBY")

    def cmd_settle(self, arguments) -> None:
        started = time.monotonic()
        self.dev.wait_settled()
        self.out(f"  settled in {time.monotonic() - started:.1f} s")

    def cmd_outq(self, arguments) -> None:
        amplitude, unit, frequency = self.dev.out_actual()
        self.out(f"  OUT?    {format_output(amplitude, unit, frequency)}")
        if unit.upper() == "OHM":
            self.out("  (in ohms this is the CHARACTERIZED value actually "
                     "sourced, not the nominal request — use it as the "
                     "error reference, guide §11.4)")

    def cmd_adjq(self, arguments) -> None:
        amplitude, unit, frequency = self.dev.adj_actual()
        self.out(f"  ADJOUT? {format_output(amplitude, unit, frequency)}")

    def cmd_uncert(self, arguments) -> None:
        value, units, days = self.dev.uncert()
        if value is None:
            self.out("  UNCERT? no specification for the present output "
                     "(the -1.0 sentinel, guide §9)")
            return
        self.out(f"  UNCERT? {format_amplitude(value, units)} "
                 f"over a {days} day interval")

    def cmd_range(self, arguments) -> None:
        self.out(f"  RANGE?  {self.dev.range_id()}")

    def _modifier(self, arguments, name: str, setter) -> None:
        self._need(arguments, 1, f"{name} on|off")
        state = parse_onoff(arguments[0])
        setter(state)
        self.out(f"  {name} {'ON' if state else 'OFF'}")

    def cmd_rangelck(self, arguments) -> None:
        self._modifier(arguments, "rangelck", self.dev.range_lock)

    def cmd_rcomp(self, arguments) -> None:
        self._modifier(arguments, "rcomp", self.dev.rcomp)

    def cmd_extsense(self, arguments) -> None:
        self._modifier(arguments, "extsense", self.dev.ext_sense)

    def cmd_extguard(self, arguments) -> None:
        self._modifier(arguments, "extguard", self.dev.ext_guard)

    def cmd_wband(self, arguments) -> None:
        if not arguments:
            self.out(f"  WBND {'on' if self.dev.is_wideband() else 'off'} "
                     f"(ISR bit 10)")
            return
        state = parse_onoff(arguments[0])
        self.dev.wband(state)
        if state:
            self.out("  WBAND ON — 50 Ω wideband output: 300 µV to 3.5 V "
                     "rms, 10 Hz to 30 MHz; 'out' now validates against the "
                     "wideband envelope.")
            self.out("  ! specifications apply at the END OF THE CABLE into "
                     "its 50 Ω termination.")
        else:
            self.out("  WBAND OFF — back to the standard output. The value "
                     "on the terminals may have been re-selected or dropped "
                     "to 0 mV dc ([OM §4-30]); check 'outq'.")

    def _is_live(self, what: str) -> bool:
        """Whether the output is in OPERATE, refusing to guess if it cannot say.

        An unreadable ``ISR?`` is not "it is in standby": the whole point of
        the gate below is the case where something is already on the
        terminals.
        """
        try:
            return bool(self.dev.query_int("ISR?") & ISR_OPER)
        except Exception as exc:
            raise ConsoleError(
                f"cannot read ISR? to see whether the output is live "
                f"({exc}); refusing to {what} blind. Type 'stby' first."
            ) from None

    def _gate_live_edit(self, what: str, amplitude: float, unit: str,
                        frequency: float = 0.0) -> None:
        """Require the typed 'yes' before an edit takes a LIVE output past a
        hazard threshold.

        ``mult``, ``incr`` and ``newref`` all change (or lock in) the value on
        the terminals without going through ``oper``, so the console's one
        advertised interlock -- "energizing >= 22 V requires a typed yes" --
        was bypassed by three commands the ``calrng``/``calwbflat`` nulling
        workflows actively encourage while the output is live.  The firmware's
        own auto-standby covers a crossing *from below* 22 V ([OM §3-x]), but
        not an escalation that starts at or above it (30 V x 10) and not
        current at all: there is no auto-standby for amplitude in amps.
        """
        reason = output_hazard(amplitude, unit)
        if not reason:
            return
        setpoint = format_output(amplitude, unit, frequency)
        banner = "HIGH CURRENT" if unit.upper() == "A" else "HIGH VOLTAGE"
        self.console.show(
            f"── {banner} ──",
            f"{what} would leave {setpoint} on LIVE terminals, which "
            f"{reason}.",
            "The output is in OPERATE now; this does not go through 'oper'.",
            panel_msg=banner)
        if not self.console.confirm_explicit(f"{what} to {setpoint}"):
            raise ConsoleError(
                f"declined — nothing was sent; the output is unchanged. "
                f"Type 'stby' first if you want to edit it de-energized.")

    def _live_output(self, what: str, *, adjusted: bool = False
                     ) -> "Optional[tuple[float, str, float]]":
        """The value on live terminals, or ``None`` when it is in standby.

        *adjusted* reads ``ADJOUT?`` -- the value an ``incr`` sequence is
        editing -- rather than ``OUT?``.
        """
        if not self._is_live(what):
            return None
        try:
            if adjusted:
                return self.dev.adj_actual()
            return self._resolved_output()
        except Exception as exc:
            raise ConsoleError(
                f"the output is in OPERATE and it could not be read back "
                f"({exc}); refusing to {what} blind. Type 'stby' first."
            ) from None

    def cmd_incr(self, arguments) -> None:
        self._need(arguments, 1, "incr <±step>[V|A|OHM|Hz]   "
                                 "(incr -10uV / incr 1Hz)")
        value, unit = parse_value(arguments[0])
        if unit != "HZ":
            # A frequency step is not an amplitude hazard; an amplitude step
            # is, and INCR applies it to the live output immediately.
            live = self._live_output("incr", adjusted=True)
            if live is not None:
                present, out_unit, out_freq = live
                self._gate_live_edit("incr", present + value, out_unit,
                                     out_freq)
        if unit == "HZ":
            self.dev.incr(value, "HZ")
        else:
            # A magnitude step is sent bare: INCR takes it in the present
            # function's own unit, and the SI prefix has already been folded
            # into the number by parse_value.
            self.dev.incr(value)
        amplitude, out_unit, frequency = self.dev.adj_actual()
        self.out(f"  ADJOUT? {format_output(amplitude, out_unit, frequency)}"
                 f"  (error mode — 'newref' locks the edit in)")

    def cmd_newref(self, arguments) -> None:
        live = self._live_output("newref")
        if live is not None:
            # NEWREF does not move the terminals ([OM Table 5-2]: "sets the
            # reference value to be the present output value"), but it makes a
            # hazardous value the reference every later mult/incr multiplies,
            # and it is only reachable with the output live.
            self._gate_live_edit("newref", *live)
        self.dev.newref()
        amplitude, unit, frequency = self.dev.out_actual()
        self.out(f"  NEWREF — reference is now "
                 f"{format_output(amplitude, unit, frequency)}")

    def cmd_mult(self, arguments) -> None:
        self._need(arguments, 1, "mult <factor>   (mult 1.9)")
        try:
            factor = float(arguments[0])
        except ValueError:
            raise ConsoleError(
                f"the MULT factor is a number, got {arguments[0]!r}"
            ) from None
        live = self._live_output("mult")
        if live is not None:
            present, out_unit, out_freq = live
            self._gate_live_edit("mult", present * factor, out_unit, out_freq)
        self.dev.mult(factor)
        amplitude, unit, frequency = self.dev.out_actual()
        self.out(f"  MULT {factor:g} — output is now "
                 f"{format_output(amplitude, unit, frequency)}")
        reason = output_hazard(amplitude, unit)
        if reason:
            self.out(f"  ! the multiplied output {reason}.")

    def cmd_limit(self, arguments) -> None:
        if not arguments:
            pos_v, neg_v, pos_a, neg_a = self.dev.limits()
            self.out(f"  LIMIT?  voltage {format_amplitude(pos_v, 'V')} to "
                     f"{format_amplitude(neg_v, 'V')}")
            self.out(f"          current {format_amplitude(pos_a, 'A')} to "
                     f"{format_amplitude(neg_a, 'A')}")
            return
        self._need(arguments, 2, "limit <+limit> <-limit>   "
                                 "(limit 25V -25V / limit 100mA -100mA)")
        positive, pos_unit = parse_amplitude(arguments[0])
        negative, neg_unit = parse_amplitude(arguments[1])
        if pos_unit != neg_unit:
            raise ConsoleError(
                f"the two limits must share a unit, got {pos_unit} and "
                f"{neg_unit} (the voltage pair and the current pair are set "
                f"separately)")
        if pos_unit == "OHM":
            raise ConsoleError("entry limits exist for V and A only")
        self.dev.set_limits(positive, negative, pos_unit)
        self.out(f"  LIMIT {format_amplitude(positive, pos_unit)}, "
                 f"{format_amplitude(negative, neg_unit)} — the calibrator "
                 f"itself now refuses anything beyond (faults 815/856).")
        self.out("  (power-up or *RST resets the limits to the full "
                 "envelope)")

    # ── status and faults ─────────────────────────────────────────────

    def cmd_status(self, arguments) -> None:
        self.out(format_status(read_status(self.dev)))

    def cmd_isr(self, arguments) -> None:
        self.out("  " + format_isr(self.dev.query_int("ISR?")).replace(
            "\n", "\n  "))

    def cmd_iscr(self, arguments) -> None:
        changed = self.dev.iscr()
        if changed:
            self.out("  ISCR? — ISR bits that CHANGED since the last read:")
            self.out("  " + format_isr(changed).replace("\n", "\n  "))
        else:
            self.out("  ISCR? 0 — nothing has changed since the last read")
        self.out("  (that read CLEARED the change register — a transient "
                 "caught here is gone from the next read)")

    def cmd_esr(self, arguments) -> None:
        self.out("  " + format_esr(self.dev.query_int("*ESR?")).replace(
            "\n", "\n  "))
        self.out("  (that read CLEARED the ESR — guide §8.3)")

    def cmd_stb(self, arguments) -> None:
        self.out("  " + format_stb(self.dev.serial_poll()).replace(
            "\n", "\n  "))
        self.out("  (a hardware serial poll also CLEARS RQS — guide "
                 "§8.2)")

    def cmd_faults(self, arguments) -> None:
        faults = self.dev.drain_faults()
        self.out(format_faults(faults))
        if faults:
            self.out("")
            self.out(format_fault_groups(faults))

    def cmd_explain(self, arguments) -> None:
        self._need(arguments, 1, "explain <fault code>")
        try:
            code = int(arguments[0])
        except ValueError:
            raise ConsoleError(
                f"{arguments[0]!r} is not a fault code (an integer such as "
                f"3111)") from None
        offline = FAULT_CODES.get(code)
        try:
            instrument = self.dev.query(f"EXPLAIN? {code}")
            instrument = instrument.strip().strip('"') or None
        except Exception as exc:
            instrument = None
            self.out(f"  (EXPLAIN? failed: {exc})")
        self.out(format_explain(code, offline, instrument))

    def cmd_state(self, arguments) -> None:
        code, substate = self.dev.state()
        self.out(f"  STATE?  {code}  {STATE_NAMES.get(code, 'unknown state')}"
                 + (f"  — {substate}" if substate else ""))

    def cmd_watch(self, arguments, *, max_polls: Optional[int] = None) -> None:
        if arguments:
            try:
                interval = float(arguments[0])
            except ValueError:
                raise ConsoleError(
                    f"the watch interval is a number of seconds, got "
                    f"{arguments[0]!r}") from None
        else:
            interval = self.watch_interval
        if interval < 0:
            raise ConsoleError("the watch interval must not be negative")
        self.out(f"  watching ISR + the fault queue every {interval:g} s; "
                 f"only changes are printed. Ctrl-C stops.")
        self.out("  (this DRAINS the fault queue and the ISCR? change "
                 "register as it goes — faults and transients are reported "
                 "here and then gone)")
        previous: Optional[int] = None
        state = {"stop": False}
        polls = 0
        with interruptible(state):
            while not state["stop"]:
                isr = self.dev.query_int("ISR?")
                changed = self.dev.iscr()
                if isr != previous:
                    self.out(f"  {time.strftime('%H:%M:%S')}  "
                             f"{format_isr_change(previous, isr)}")
                if previous is not None:
                    # Latched changes the poll-to-poll comparison never saw:
                    # a bit that flipped and flipped back between polls. The
                    # first poll is skipped -- the register may hold changes
                    # from before the watch began.
                    transient = changed & ~(previous ^ isr)
                    if transient:
                        names = [name for name, on
                                 in decode_isr(transient).items() if on]
                        self.out(f"  {time.strftime('%H:%M:%S')}  transient "
                                 f"(came and went between polls): "
                                 + ", ".join(names))
                previous = isr
                faults = self.dev.drain_faults()
                if faults:
                    self.out(f"  {time.strftime('%H:%M:%S')}  new faults:")
                    for code, text in faults:
                        self.out(f"    {format_fault(code, text)}")
                polls += 1
                if max_polls is not None and polls >= max_polls:
                    break
                if interval:
                    time.sleep(interval)
        self.out("  watch stopped")

    # ── long-term command guards ──────────────────────────────────────

    def _require_output_safe(self, what: str) -> None:
        """Refuse a long-term command while a dangerous output is live.

        Raises :class:`ConsoleError` when *what* must not start; returns
        normally when it may. The driver deliberately does not enforce this;
        it is the console's job, and it matters because there is no remote
        abort — ``STBY`` during a long-term command is itself rejected with
        fault 2002.
        """
        try:
            isr = self.dev.query_int("ISR?")
        except Exception as exc:
            raise ConsoleError(
                f"cannot read ISR? to check the output state ({exc}); "
                f"refusing to start {what} blind.") from exc
        if not isr & ISR_OPER:
            return
        try:
            amplitude, unit, frequency = self._resolved_output()
        except Exception as exc:
            raise ConsoleError(
                f"the output is in OPERATE and OUT? failed ({exc}); refusing "
                f"to start {what} blind. Type 'stby' first.") from exc
        reason = output_hazard(amplitude, unit)
        if reason:
            raise ConsoleError(
                interlock_refusal(what, amplitude, unit, frequency, reason))

    def _announce_long_term(self, what: str) -> None:
        banner, body = long_term_warning(what)
        self.console.show(banner, *body, panel_msg="LONG TERM CMD")

    def _progress(self):
        """A progress callback that prints only when the state changes."""
        last = [None]

        def callback(code, substate):
            key = (code, substate)
            if key == last[0]:
                return
            last[0] = key
            name = STATE_NAMES.get(code, "unknown state"
                                   if code != f57.STATE_UNKNOWN
                                   else "progress poll failed")
            self.out(f"    STATE {code} {name}"
                     + (f" — {substate}" if substate else ""))
        return callback

    # ── diagnostics ───────────────────────────────────────────────────

    def cmd_test(self, arguments) -> None:
        self._require_output_safe("*TST?")
        self._announce_long_term("*TST? self-test")
        self.out("  *TST? blocks the bus for its whole duration and its "
                 "runtime is undocumented (gap G9); timing it.")
        started = time.monotonic()
        with deferred_sigint("*TST?") as interrupt:
            passed = self.dev.self_test()
        elapsed = time.monotonic() - started
        self.out(f"  *TST? {'PASSED' if passed else 'FAILED'} in "
                 f"{elapsed:.1f} s")
        if interrupt["hit"]:
            self.out("  (a Ctrl-C during the test was ignored on purpose)")
        faults = self.dev.drain_faults()
        if faults:
            self.out("  self-test faults (4300-level):")
            self._report_faults(faults)
        elif not passed:
            self.out("  ! *TST? answered 'fail' but the fault queue is "
                     "empty.")

    def cmd_diag(self, arguments) -> None:
        target = (arguments[0].upper() if arguments else "D5700")
        if target not in f57.DIAG_TARGETS:
            raise ConsoleError(
                f"unknown DIAG target {target!r}; expected one of "
                f"{', '.join(f57.DIAG_TARGETS)}")
        self._require_output_safe(f"DIAG {target}")
        self._announce_long_term(f"DIAG {target}")
        started = time.monotonic()
        with deferred_sigint(f"DIAG {target}") as interrupt:
            faults = self.dev.diag(target, progress_cb=self._progress(),
                                   poll_interval=self.poll_interval)
        ending = ("HALTED at a fault" if self.dev.diag_halted()
                  else "finished")
        self.out(f"  DIAG {target} {ending} after "
                 f"{time.monotonic() - started:.1f} s")
        if interrupt["hit"]:
            self.out("  (a Ctrl-C during the run was ignored on purpose)")
        self._report_faults(faults)
        if faults:
            self.out("")
            self._report_faults(faults, grouped=True)
        if self.dev.diag_halted():
            self.out("  the diagnostic is HALTED and waiting. 'diagstep' is "
                     "the stepping workflow; 'raw DIAG ABORT' releases it.")

    def cmd_diagstep(self, arguments) -> None:
        """DIAGFLT HALT + step from fault to fault — the A14 workflow."""
        target = (arguments[0].upper() if arguments else "D5700")
        if target not in f57.DIAG_TARGETS:
            raise ConsoleError(
                f"unknown DIAG target {target!r}; expected one of "
                f"{', '.join(f57.DIAG_TARGETS)}")
        self._require_output_safe(f"DIAG {target} (stepping)")
        self._announce_long_term(f"DIAG {target}, stepping at every fault")
        seen: "list[tuple[int, str]]" = []
        ending = "did not start"
        try:
            # The context manager restores the nonvolatile DIAGFLT setting on
            # every exit path, this method's own exceptions included.
            with self.dev.diagflt_temporarily("HALT") as previous:
                self.out(f"  DIAGFLT was {previous}; HALT is selected for "
                         f"this run and {previous} is restored when it ends.")
                with deferred_sigint(f"DIAG {target}"):
                    try:
                        ending = self._step_through(target, seen)
                    except BaseException as exc:
                        # BaseException on purpose: EOF at the halt prompt
                        # arrives as KeyboardInterrupt, and a DIAG CONT
                        # timeout on a sick unit is the other likely caller.
                        # Either way the diagnostic is *waiting* inside the
                        # calibrator, and walking away from it is not an
                        # option -- nor is recording "ran to the end" for a
                        # run that did not.
                        ending = (f"INTERRUPTED ({type(exc).__name__}) — "
                                  f"{self._release_halt(seen)}")
                        raise
        finally:
            # In a `finally` because the fault list is the whole point of the
            # exercise: an exception on the way out — including one from the
            # DIAGFLT restore — must not cost the operator what was found.
            self._diagstep_summary(target, seen, ending)

    def _release_halt(self, seen: list) -> str:
        """Send ``DIAG ABORT`` if a diagnostic is halted; report what happened.

        ``DIAG`` is the one long-term command with a documented abort
        (guide §19.2 gap G6), so this is the only way to release a calibrator
        that is stopped inside a diagnostic. Best effort: the caller is
        already handling a failure and must not be derailed by a second one.
        """
        if not self.dev.diag_halted():
            return "the diagnostic was not halted"
        self.out("  releasing the halted diagnostic with DIAG ABORT")
        try:
            seen += self.dev.diag_abort()
        except Exception as exc:
            self.out(f"  ! DIAG ABORT failed ({exc}). The calibrator is "
                     f"probably STILL HALTED inside the diagnostic — clear it "
                     f"from the front panel before doing anything else.")
            return "DIAG ABORT FAILED, the calibrator may still be halted"
        return "the calibrator was released with DIAG ABORT"

    def _step_through(self, target: str, seen: list) -> str:
        """Run the diagnostic and step from fault to fault; return the ending.

        The returned string is what the summary records, so an ending is only
        ever claimed by the code path that actually reached it.
        """
        seen += self.dev.diag(target, progress_cb=self._progress(),
                              poll_interval=self.poll_interval)
        while self.dev.diag_halted():
            shown = seen[-4:]
            trimmed = ("" if len(shown) == len(seen)
                       else f" (last {len(shown)} of {len(seen)})")
            self.console.show(
                "── diagnostic HALTED at a fault ──",
                f"faults so far{trimmed}:",
                *[f"  {format_fault(code, text)}" for code, text in shown],
                "",
                "Enter or 'y'  → DIAG CONT: step to the next fault",
                "anything else → DIAG ABORT: stop the diagnostic here",
                "",
                "Ctrl-C is deferred here on purpose: the diagnostic is "
                "halted",
                "inside the calibrator and one of these two answers is what "
                "releases it.",
                panel_msg="DIAG HALTED")
            if not self.console.confirm_ok(
                    "stepping to the next fault (DIAG CONT)",
                    hint="[Enter or y = continue / anything else = abort]"):
                seen += self.dev.diag_abort()
                return "ABORTED by the operator"
            seen += self.dev.diag_cont(progress_cb=self._progress(),
                                       poll_interval=self.poll_interval)
        return "ran to the end"

    def _diagstep_summary(self, target: str, seen: list,
                          ending: str) -> None:
        self.out("")
        self.out(f"── diagstep summary — DIAG {target} {ending} ──")
        self.out(format_faults(seen))
        if seen:
            self.out("")
            self.out(format_fault_groups(seen))

    def cmd_diagflt(self, arguments) -> None:
        if not arguments:
            self.out(f"  DIAGFLT? {self.dev.diagflt_query()}")
            self.out("  (HALT = stop and wait at each fault, ABORT = stop, "
                     "CONT = carry on)")
            return
        mode = arguments[0].upper()
        if mode not in f57.DIAGFLT_MODES:
            raise ConsoleError(
                f"unknown DIAGFLT mode {mode!r}; expected one of "
                f"{', '.join(f57.DIAGFLT_MODES)}")
        self.dev.diagflt(mode)
        self.out(f"  DIAGFLT {mode}")
        self.out(f"  ! this setting lives in NONVOLATILE MEMORY. It survives "
                 f"the power cycle and changes what the FRONT PANEL does too; "
                 f"the factory value is {f57.DIAGFLT_DEFAULT}. 'diagstep' "
                 f"sets HALT only for one run and puts the old value back.")

    def cmd_fatality(self, arguments) -> None:
        if not self.service:
            raise ConsoleError(
                "FATALITY? is service-only (guide §16); restart the "
                "console with --service")
        for line in self.dev.fatality():
            self.out(f"  {line}")

    def cmd_fatalclr(self, arguments) -> None:
        if not self.service:
            raise ConsoleError(
                "FATALCLR is service-only (guide §16); restart the "
                "console with --service")
        if not self.console.confirm_explicit(
                "clearing the fatal-fault list (it is the unit's only "
                "permanent failure history)"):
            raise ConsoleError("declined — nothing was cleared.")
        self.dev.fatalclr()
        self.out("  fatal-fault list cleared")

    # ── calibration and housekeeping (read-only) ──────────────────────

    def cmd_caldays(self, arguments) -> None:
        selectors = ([arguments[0].upper()] if arguments
                     else ["CAL", "ZERO"])
        for selector in selectors:
            days = self.dev.cal_days(selector)
            self.out(f"  CAL_DAYS? {selector:<6} {days} days")
            if selector == "ZERO" and days > ZERO_CAL_MAX_DAYS:
                self.out(f"  ! the dc zeros are past their {ZERO_CAL_MAX_DAYS}"
                         f" day limit (fault 1303 territory); specifications "
                         f"do not apply until 'calzero' is run.")

    def cmd_caldate(self, arguments) -> None:
        fmt = self.dev.datefmt()
        selectors = ([arguments[0].upper()] if arguments
                     else ["CAL", "ZERO"])
        for selector in selectors:
            self.out(f"  CAL_DATE? {selector:<6} "
                     f"{self.dev.cal_date(selector)}  (field order {fmt}; the "
                     f"two-digit year has no century window)")

    def cmd_calintv(self, arguments) -> None:
        if not arguments:
            self.out(f"  CAL_INTV? {self.dev.cal_intv()} days")
            return
        try:
            days = int(arguments[0])
        except ValueError:
            raise ConsoleError(
                f"the interval is 1, 90, 180 or 365 days, got "
                f"{arguments[0]!r}") from None
        self.dev.set_cal_intv(days)
        self.out(f"  CAL_INTV {days} — saved in NONVOLATILE memory; every "
                 f"UNCERT? from now on answers for this interval.")

    def cmd_calconf(self, arguments) -> None:
        if not arguments:
            self.out(f"  CAL_CONF? {self.dev.cal_conf()}")
            return
        self.dev.set_cal_conf(arguments[0])
        self.out(f"  CAL_CONF {arguments[0]} — saved in NONVOLATILE memory; "
                 f"every UNCERT? from now on answers at this confidence.")

    def cmd_calconst(self, arguments) -> None:
        self._need(arguments, 1, "calconst <name> [group]")
        group = arguments[1].upper() if len(arguments) > 1 else "CAL"
        name = arguments[0]
        self.out(f"  CAL_CONST? {group},{name} = "
                 f"{self.dev.cal_const(group, name):.10E}")

    def cmd_calshift(self, arguments) -> None:
        self._need(arguments, 2, "calshift <group> <range>  "
                                 "(e.g. calshift CAL DC220V)")
        shift = self.dev.cal_shift(arguments[0].upper(), arguments[1].upper())
        self.out(self._format_shift(shift))

    def cmd_calslst(self, arguments) -> None:
        group = arguments[0].upper() if arguments else "CAL"
        for shift in self.dev.cal_slst(group):
            self.out(self._format_shift(shift))

    @staticmethod
    def _format_shift(shift) -> str:
        lines = [f"  {shift.range_id}  ({len(shift.points)} point(s))",
                 "     magnitude       freq       abs shift    rel ppm   "
                 "% of spec  spec ppm"]
        for point in shift.points:
            lines.append(
                f"    {point.magnitude:>12.6G}  {point.frequency_hz:>9.4G}  "
                f"{point.absolute_shift:>12.4E}  "
                f"{point.relative_shift_ppm:>8.3f}  "
                f"{point.percent_of_spec:>9.2f}  {point.spec_ppm:>8.3f}")
        return "\n".join(lines)

    def cmd_calrpt(self, arguments) -> None:
        if arguments and arguments[0].lower() == "summary":
            from caldmm.cal.calreport import format_summary, parse_report
            group = arguments[1].upper() if len(arguments) > 1 else "CAL"
            text = self.dev.cal_rpt(group)
            try:
                report = parse_report(text)
            except ValueError as exc:
                raise ConsoleError(
                    f"CAL_RPT? {group} returned no parseable report "
                    f"({exc}); run 'calrpt {group}' for the raw text"
                ) from None
            self.out("  " + format_summary(report).replace("\n", "\n  "))
            return
        group = arguments[0].upper() if arguments else "CAL"
        self.out(self.dev.cal_rpt(group))

    def cmd_calzero(self, arguments) -> None:
        self._long_term_cal("CAL_ZERO", self.dev.cal_zero,
                            "the dc zeros calibration (~2.5 minutes)")

    def cmd_calchk(self, arguments) -> None:
        self._long_term_cal(
            "CAL_CHK", self.dev.cal_chk,
            "the calibration check against the internal standards "
            "(no constant is changed)")

    def _long_term_cal(self, name: str, method, description: str) -> None:
        self._require_output_safe(name)
        self._announce_long_term(f"{name} — {description}")
        if not self.console.confirm_explicit(f"starting {name}"):
            raise ConsoleError(f"declined — {name} was not sent.")
        started = time.monotonic()
        with deferred_sigint(name) as interrupt:
            method(progress_cb=self._progress())
        self.out(f"  {name} completed in {time.monotonic() - started:.1f} s")
        if interrupt["hit"]:
            self.out("  (a Ctrl-C during the run was ignored on purpose)")
        self._report_faults(self.dev.drain_faults())

    def cmd_caltemp(self, arguments) -> None:
        if not arguments:
            for what in ("CAL", "CHECK", "WBGAIN", "WBFLAT"):
                try:
                    value = self.dev.cal_temp_query(what)
                except Exception as exc:
                    self.out(f"  CAL_TEMP? {what:<7} (no answer: {exc})")
                    continue
                self.out(f"  CAL_TEMP? {what:<7} {value:.1f} C")
            return
        try:
            celsius = float(arguments[0].rstrip("Cc"))
        except ValueError:
            raise ConsoleError(
                f"the calibration temperature is a number of °C, got "
                f"{arguments[0]!r}") from None
        self.dev.cal_temp(celsius)
        self.out(f"  CAL_TEMP {celsius:g} — used for every calibration "
                 f"activity until changed. Set it BEFORE calzero / calchk / "
                 f"calwbgain; unset, the calibrator assumes 23 °C.")

    def cmd_calrng(self, arguments) -> None:
        usage = ("calrng <RANGE_ID> <magnitude> [freq] | calrng null | "
                 "calrng store")
        if not self.service:
            raise ConsoleError(
                "calrng changes range-calibration constants; restart with "
                "--service if you really mean to (guide §14.8)")
        self._need(arguments, 1, usage)
        head = arguments[0].upper()
        if head in ("NULL", "STORE"):
            if head == "STORE" and not self.console.confirm_explicit(
                    "committing range-calibration constants to EEPROM "
                    "(CALIBRATION switch must be in ENABLE)"):
                raise ConsoleError("declined — CAL_RNG STORE was not sent.")
            self.dev.cal_rng(head)
            self.out(f"  CAL_RNG {head}"
                     + ("" if head == "STORE"
                        else " — constant computed from the nulled output"))
            return
        self._need(arguments, 2, usage)
        magnitude, _unit = parse_value(arguments[1])
        frequency = (parse_frequency(arguments[2])
                     if len(arguments) > 2 else None)
        self.dev.cal_rng(head, magnitude, frequency)
        self.out(f"  CAL_RNG {head} staged at {magnitude:g} — 'oper', then "
                 f"'incr' until the external standard nulls, then "
                 f"'calrng null'; 'calrng store' commits (guide §14.8)")

    def cmd_calwbgain(self, arguments) -> None:
        self._need(arguments, 1, "calwbgain pgain|ngain|store")
        step = arguments[0].upper()
        if step == "STORE":
            if not self.console.confirm_explicit(
                    "committing wideband gain constants to EEPROM "
                    "(CALIBRATION switch must be in ENABLE)"):
                raise ConsoleError(
                    "declined — CAL_WBGAIN STORE was not sent.")
            self.dev.cal_wbgain("STORE")
            self.out("  CAL_WBGAIN STORE")
            return
        if step not in ("PGAIN", "NGAIN"):
            raise ConsoleError("calwbgain pgain|ngain|store")
        side = "HI" if step == "PGAIN" else "LO"
        self._long_term_cal(
            f"CAL_WBGAIN {step}",
            lambda **kwargs: self.dev.cal_wbgain(step, **kwargs),
            f"wideband gain, {step.lower()} — the wideband cable's center "
            f"conductor must be on SENSE {side} (set 'caltemp' first)")

    def cmd_calwbflat(self, arguments) -> None:
        self._need(arguments, 1, "calwbflat start|null|store")
        step = arguments[0].upper()
        if step == "STORE":
            if not self.console.confirm_explicit(
                    "committing wideband flatness constants to EEPROM "
                    "(CALIBRATION switch must be in ENABLE)"):
                raise ConsoleError(
                    "declined — CAL_WBFLAT STORE was not sent.")
            self.dev.cal_wbflat("STORE")
            self.out("  CAL_WBFLAT STORE")
            return
        if step not in ("START", "NULL"):
            raise ConsoleError("calwbflat start|null|store")
        self.dev.cal_wbflat(step)
        amplitude, unit, frequency = self.dev.out_actual()
        if (amplitude, unit.upper(), frequency) == (0.0, "V", 0.0):
            self.out(f"  CAL_WBFLAT {step} — OUT? returned 0,V,0: every "
                     f"flatness point is calibrated. 'calwbflat store' "
                     f"commits.")
            return
        self.out(f"  CAL_WBFLAT {step} — now sourcing "
                 f"{format_output(amplitude, unit, frequency)}. 'oper', "
                 f"'incr' until the external standard nulls, then "
                 f"'calwbflat null' for the next point.")

    def cmd_calclst(self, arguments) -> None:
        which = arguments[0].upper() if arguments else "CAL"
        constants = self.dev.cal_clst(which)
        self.out(f"  CAL_CLST? {which} — {len(constants)} constants")
        for name, value in constants.items():
            self.out(f"    {name:<24} {value:.10E}")

    def cmd_ohmsref(self, arguments) -> None:
        which = arguments[0].upper() if arguments else "CAL"
        value = self.dev.ohms_ref(which)
        self.out(f"  OHMSREF? {which} — internal resistance reference "
                 f"{value:.10E}")

    def cmd_local(self, arguments) -> None:
        self.dev.local()
        self.out("  GTL sent — the front panel is back in local control. "
                 "The next command from this console re-asserts remote.")

    # ── identity and housekeeping ─────────────────────────────────────

    def cmd_idn(self, arguments) -> None:
        identity = self.dev.identity
        if identity is None:
            self.out("  *IDN? (not read)")
            return
        self.out(f"  {identity.raw}")
        self.out(f"  {identity.manufacturer} {identity.model}  "
                 f"s/n {identity.serial}  firmware {identity.firmware}")

    def cmd_opt(self, arguments) -> None:
        options = getattr(self.dev, "options", [])
        self.out(f"  *OPT? {', '.join(options) if options else '(none)'}")

    def cmd_etime(self, arguments) -> None:
        minutes = self.dev.etime()
        self.out(f"  ETIME?  {minutes} min  ({minutes / 60.0:.1f} h "
                 f"lifetime)")

    def cmd_ontime(self, arguments) -> None:
        minutes = self.dev.ontime()
        self.out(f"  ONTIME? {minutes} min since power-up")
        if minutes < f57.WARMUP_MAX_MINUTES:
            self.out(f"  ! specifications need twice the time since last "
                     f"warmed up, up to {f57.WARMUP_MAX_MINUTES} min "
                     f"(guide §11.6)")

    def cmd_clock(self, arguments) -> None:
        time_field, date_field = self.dev.clock()
        self.out(f"  CLOCK?  {time_field} {date_field}  "
                 f"(DATEFMT? {self.dev.datefmt()})")

    def cmd_rptstr(self, arguments) -> None:
        self.out(f"  RPT_STR? {self.dev.rpt_str()!r}")

    def cmd_lrn(self, arguments) -> None:
        self.out(f"  {self.dev.lrn()}")
        self.out("  ! *LRN? captures OPERATE/STANDBY too: writing this string "
                 "back can ENERGIZE the output (guide §14.10). Send STBY "
                 "first and read it before you replay it.")

    def cmd_echo(self, arguments) -> None:
        self._need(arguments, 1, "echo <text>")
        text = " ".join(arguments)
        answer = self.dev.echo(text)
        self.out(f"  ECHO? -> {answer!r}"
                 + ("" if answer == text
                    else "  ! the calibrator did not echo it verbatim"))

    # ── escape hatch ──────────────────────────────────────────────────

    def cmd_raw(self, arguments) -> None:
        self._need(arguments, 1, "raw <command...>")
        command = " ".join(arguments)
        # The gate keys on the head token alone. A trailing '?' decides
        # nothing here: it only picks read-back below. 'raw CAL_STORE ?' ends
        # in a question mark and is still the command that burns EEPROM, so
        # letting the '?' waive the gate would have been a hole straight
        # through it. A genuine query carries the '?' on its head token
        # ('ETIME?'), which is a different token from the gated 'ETIME'.
        head = arguments[0].upper()
        if not self.service and (head in RAW_SERVICE_ONLY
                                 or head.startswith("FORMAT")):
            raise ConsoleError(
                f"'raw {command}' writes calibration memory, energizes the "
                f"output or is service-only, and this console is not in "
                f"service mode. Restart with --service if you really mean it "
                f"— or use the dedicated command, which has the "
                f"confirmation and the interlock.")
        is_query = command.rstrip().endswith("?")
        self.out(f"  -> {command}")
        if is_query:
            try:
                self.out(f"  <- {self.dev.query(command)}")
            except Fluke5700AError as exc:
                self.out(f"  <- (no answer: {exc})")
                self.out("  an unknown QUERY produces no response at all "
                         "rather than a fault (gap G31), so a timeout here "
                         "usually means the spelling is wrong.")
        else:
            self.dev.cmd(command)
        faults = self.dev.drain_faults()
        self._report_faults(faults)

    def cmd_help(self, arguments) -> None:
        self.out(format_help())


# ──────────────────────────────────────────────────────────────────────────
# Startup banner
# ──────────────────────────────────────────────────────────────────────────

def read_banner(dev) -> str:
    """Read the startup context and render it; failures degrade to '(n/a)'."""

    def safe(read):
        try:
            return read()
        except Exception:
            return None

    return format_banner(
        getattr(dev, "identity", None),
        getattr(dev, "options", []),
        safe(dev.ontime),
        safe(lambda: dev.cal_days("ZERO")),
        safe(lambda: dev.cal_days("CAL")),
        safe(dev.cal_intv))


# ──────────────────────────────────────────────────────────────────────────
# Entry point
# ──────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="caldmm-5700a-console",
        description="Fluke 5700A/5720A service and manual console: "
                    "diagnostics, fault triage and manual sourcing.",
        epilog="With no action flag this starts a REPL; type 'help' there. "
               "Multiplier prefixes are strict SI and case-sensitive "
               "(m=milli, M=mega) in both modes. Exit status: 0 if every "
               "requested action succeeded, 1 if one was refused or failed "
               "(the rest are then abandoned), 2 on a bad flag combination.")
    link = parser.add_mutually_exclusive_group()
    # default=None, not DEFAULT_GPIB_ADDRESS, and resolved in run(). argparse
    # before 3.12 only counts an argument towards a mutually exclusive group
    # when its value *is not* the declared default, so with the default spelled
    # here `--address 4 --port /dev/ttyUSB0` is silently accepted on Python
    # 3.11 and the port quietly wins.
    link.add_argument("--address", type=int, default=None,
                      help=f"GPIB address (default "
                           f"{f57.DEFAULT_GPIB_ADDRESS})")
    link.add_argument("--port", metavar="DEV",
                      help="use the rear-panel RS-232C port at this device "
                           "path (e.g. /dev/ttyUSB0 or COM3) instead of GPIB")
    parser.add_argument("--bus", type=int, default=f57.DEFAULT_GPIB_BUS,
                        help="GPIB bus/board index (default %(default)s); "
                             "GPIB only")
    parser.add_argument("--baud", type=int, default=f57.DEFAULT_SERIAL_BAUD,
                        choices=f57.SERIAL_BAUD_RATES,
                        help="serial baud rate (default %(default)s); "
                             "only with --port")
    parser.add_argument("--timeout", type=float,
                        default=f57.DEFAULT_TIMEOUT_SEC,
                        help="session timeout in seconds "
                             "(default %(default)s)")
    parser.add_argument("--service", action="store_true",
                        help="allow the guide §16 service-only commands and "
                             "the dangerous 'raw' writes")
    parser.add_argument("--no-banner", action="store_true",
                        help="skip the identity / warm-up / zeros screen")

    one_shot = parser.add_argument_group(
        "one-shot actions (any of these runs and exits)")
    one_shot.add_argument("--out", metavar="VALUE",
                          help="source a value, e.g. 10V, 100mV, 1.5mA")
    one_shot.add_argument("--ohms", metavar="VALUE",
                          help="source a resistance, e.g. 10k, 1.9M, 0")
    one_shot.add_argument("--freq", metavar="FREQ",
                          help="frequency for --out, e.g. 1kHz")
    one_shot.add_argument("--operate", action="store_true",
                          help="energize after --out/--ohms (dropped back to "
                               "standby when the session closes)")
    one_shot.add_argument("--status", action="store_true",
                          help="print the status screen")
    one_shot.add_argument("--faults", action="store_true",
                          help="drain and print the fault queue")
    one_shot.add_argument("--diag", nargs="?", const="D5700", metavar="TARGET",
                          help="run a diagnostic (default D5700)")
    return parser


def _one_shot_requested(args) -> bool:
    return any((args.out, args.ohms, args.operate, args.status, args.faults,
                args.diag))


#: The terminations that are **not** Ctrl-C and used to leave the output live:
#: an ssh drop (SIGHUP), a ``timeout``, a ``kill``, a systemd
#: stop (SIGTERM).  ``setup_graceful_shutdown`` installs a SIGINT handler
#: only, and the default disposition of these two ends the interpreter without
#: unwinding, so ``run``'s ``finally`` -- the STBY -- never happened.
TERMINATION_SIGNALS = tuple(
    getattr(signal, name) for name in ("SIGTERM", "SIGHUP")
    if hasattr(signal, name))


def install_termination_handlers() -> "dict":
    """Turn SIGTERM/SIGHUP into an unwind, so the STBY cleanup runs.

    The handler **raises** rather than calling the cleanup itself: unwinding
    runs every ``finally`` on the stack, which is where the output is dropped
    and the device closed, and it does so on the main thread instead of
    re-entering GPIB I/O from a signal handler.  Idempotent cleanup makes the
    double-drop harmless.

    A calibrator can hold 1100 V on its terminals ([OM Safety Summary]); the
    process going away is not a reason for that to survive it.

    Returns the dispositions that were in force, for
    :func:`restore_termination_handlers`: this is a library function as much
    as an app's, and a handler that outlives the session it was installed for
    would raise ``SystemExit`` inside somebody else's program -- a test
    runner's, most often.
    """
    def handler(signum, frame):                    # pragma: no cover - signal
        name = getattr(signal.Signals(signum), "name", str(signum))
        print(f"\nCaught {name}, dropping the output to standby...",
              file=sys.stderr, flush=True)
        raise SystemExit(1)

    previous = {}
    for signum in TERMINATION_SIGNALS:
        try:
            previous[signum] = signal.getsignal(signum)
            signal.signal(signum, handler)
        except (ValueError, OSError):              # pragma: no cover - thread
            previous.pop(signum, None)
    return previous


def restore_termination_handlers(previous: "dict") -> None:
    """Put back what :func:`install_termination_handlers` displaced."""
    for signum, disposition in (previous or {}).items():
        try:
            signal.signal(signum, disposition)
        except (ValueError, OSError, TypeError):   # pragma: no cover - thread
            pass


def run(args, *, dev=None) -> int:
    """Open the session, then run either the one-shot actions or the REPL."""
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
                                 timeout=args.timeout, service=args.service)
            else:
                dev = Fluke5700A(address, args.bus, timeout=args.timeout,
                                 service=args.service)
        except Exception as exc:
            print(f"cannot open the calibrator at {where}: {exc}",
                  file=sys.stderr)
            return 1

    session = ServiceConsole(dev, OperatorConsole.for_instrument(dev),
                             service=args.service)

    closed = {"done": False}

    def cleanup():
        if closed["done"]:
            return
        closed["done"] = True
        try:
            dev.standby()
        except Exception as exc:               # noqa: BLE001 - safety path
            # Never re-raised (this runs from a signal handler and from the
            # way out), but never *silent* either: the driver's own
            # ``_safe_standby`` prints exactly this, because "the output may
            # still be live" outranks a tidy exit.
            print(f"5700A: STANDBY VERIFICATION FAILED - the output may "
                  f"still be live: {exc}", file=sys.stderr)
        try:
            if owns_device:
                dev.close()
        except Exception:
            pass

    setup_graceful_shutdown(cleanup)
    previous_signals = install_termination_handlers()

    try:
        if dev.startup_faults:
            print("  faults found in the queue at startup:")
            print(format_faults(dev.startup_faults))
        if not args.no_banner:
            print(read_banner(dev))
        if _one_shot_requested(args):
            return _run_one_shot(session, args)
        _run_repl(session)
        return 0
    finally:
        cleanup()
        restore_termination_handlers(previous_signals)


def one_shot_actions(args) -> "list[str]":
    """The console command lines *args* asks for, in a fixed, safe order.

    Sourcing comes before energizing, and both come before anything that
    reads — so ``--out 10V --status`` reports the value it just staged.
    """
    actions: "list[str]" = []
    if args.out:
        actions.append(f"out {args.out}"
                       + (f" {args.freq}" if args.freq else ""))
    if args.ohms:
        actions.append(f"ohms {args.ohms}")
    if args.operate:
        actions.append("oper")
    if args.status:
        actions.append("status")
    if args.faults:
        actions.append("faults")
    if args.diag:
        actions.append(f"diag {args.diag}")
    return actions


def _run_one_shot(session: ServiceConsole, args) -> int:
    """Execute the flag-driven actions, stopping at the first failure.

    **A failed step abandons the rest and exits non-zero.** The whole point of
    one-shot mode is scripting, and a script that cannot tell "sourced 100 V"
    from "refused to source 100 V" will energize whatever was staged before
    it — which is exactly the accident this console exists to prevent. So an
    ``oper`` never runs after a failed ``out``, and the exit status is the
    caller's evidence.
    """
    if args.operate and not (args.out or args.ohms):
        print("  ! --operate needs --out or --ohms")
        return 2
    for command in one_shot_actions(args):
        session.execute(command)
        if session.last_error is not None:
            print(f"  ! {command!r} did not succeed — abandoning the "
                  f"remaining one-shot actions and exiting 1.")
            return 1
        if command == "oper":
            print("  the session closes on exit, and closing sends STBY: "
                  "use the REPL to hold an output live.")
    return 0


def _run_repl(session: ServiceConsole) -> None:
    try:
        import readline                        # noqa: F401  (line editing)
    except ImportError:
        pass
    print("  REPL: 'help' for commands, 'quit' to leave.")
    while True:
        try:
            # Scoped to the prompt only: while a command runs, the graceful
            # shutdown handler (or a long-term command's deferred one) is
            # back in force.
            with prompt_sigint():
                line = input("5700A> ")
        except EOFError:
            print()
            break
        except KeyboardInterrupt:
            print("\n  (Ctrl-C cancels the line; type 'quit' to leave)")
            continue
        if not session.execute(line):
            break


def main():
    return run(build_parser().parse_args())


if __name__ == "__main__":
    sys.exit(main() or 0)

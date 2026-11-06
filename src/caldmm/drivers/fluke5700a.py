"""Fluke 5700A / 5720A Series II multifunction calibrator (IEEE-488, RS-232C).

The calibrator is a **source**, not a meter, so this driver deliberately does
*not* subclass :class:`caldmm.drivers._base.MultimeterBase`; like
``b2900.py`` / ``e36100.py`` / ``sdg2000x.py`` it is a plain class with an
injectable transport.

Syntax and behaviour are taken from the *Fluke 5700A / 5720A Series II — GPIB
Remote Programming Guide* (a consolidated transcription, not shipped here),
which consolidates the Operator Manual, the Remote Programming Reference, the
Service Manual and the manual supplements. Section numbers cited in the code
(``guide §8.4``) refer to that document; where its sources disagree the guide's
own errata table (§13) and known-gaps table (§19) are the tie-breakers and are
cited by item number (``§13 item 31``, ``gap G22``).

Scope of this module (all three parts):

* the :class:`Transport` seam and its two implementations —
  :class:`GpibTransport` (including the linux-gpib **T-code** timeout
  conversion, which is the single easiest thing to get wrong — ``timeout=``
  is an enum, not seconds) and :class:`SerialTransport` for the rear-panel
  RS-232C port, where Device Clear is Control/C, ``*STB?`` stands in for the
  serial poll, and Remote is a command rather than a bus line (guide §2.5),
* the exception hierarchy,
* pure response parsers and status-register decoders (no I/O — separately
  testable),
* :meth:`Fluke5700A.drain_faults`, the fault-queue drain that guide §8.6 makes
  mandatory: ``OPER`` is silently inhibited for outputs ≥ 22 V while the fault
  queue is non-empty,
* session open / close per guide §14.1 and §14.4,
* an offline :data:`FAULT_CODES` table so faults can be decoded with no
  instrument attached and when a sick unit's own ``EXPLAIN?`` misbehaves,
* **output control** — :meth:`Fluke5700A.set_output`,
  :meth:`Fluke5700A.operate`, :meth:`Fluke5700A.standby`,
  :meth:`Fluke5700A.wait_settled`, the readback queries and the modifier
  setters,
* the **offline envelope**: :func:`validate_output`,
  :func:`expected_settle_seconds` and :func:`expected_range_id` are pure
  functions, so a whole test plan can be checked with no instrument attached,
* the **long-term command runner** :meth:`Fluke5700A._run_long_term`, which
  drives ``CAL_ADJ`` / ``CAL_CHK`` / ``CAL_REF`` / ``CAL_WBGAIN`` /
  ``CAL_ZERO`` / ``DIAG`` with the non-blocking ``*OPC`` + ``*ESR?``-polling
  form of guide §7.5,
* **diagnostics** — :meth:`Fluke5700A.diag`, :meth:`Fluke5700A.diagflt`,
  :meth:`Fluke5700A.diagflt_temporarily`,
  :meth:`Fluke5700A.self_test` and
  the service-only fatal-fault list,
* the **calibration command set** (``CAL_TEMP``, ``CAL_ZERO``, ``CAL_CHK``,
  ``CAL_REF``, ``CAL_ADJ``, ``CAL_STORE``, ``CAL_RNG``) and the report /
  constant readers,
* **housekeeping** — clock, elapsed/on time, report string, protected user
  data, ``ECHO?`` and ``*LRN?``.

**This instrument sources up to 1100 V rms.** Five rules are enforced in code,
each taken from a worked failure in the guide:

1. amplitude and frequency always travel together in one ``OUT`` (§7.3) —
   ``OUT 1V,1MHZ`` followed by ``OUT 100V ; OUT 100HZ`` faults, because the
   intermediate state is 100 V at 1 MHz;
2. range lock is cleared **before** a range change, never after (§7.3);
3. a frequency is never sent in the ohms function — fault 812 (§14.2);
4. the fault queue is drained immediately before **every** ``OPER`` and ISR
   bit 0 is verified afterwards, because ``OPER`` is *silently* inhibited for
   outputs ≥ 22 V while the queue is non-empty (§8.6) — a driver that skips
   the post-check records a meter reading zero volts as a 100 % error;
5. numbers go out as plain exponentials with base units (``V``, ``A``,
   ``OHM``, ``HZ``); multiplier prefixes are never synthesized, because ``MA``
   alone is milliamps but ``MAOHM``/``MAHZ`` mean mega (§3.6).

Typical use::

    with Fluke5700A(gpib_address=4) as cal:   # opens the session in __init__
        cal.set_output(10.0, "V")             # sourced, still in standby
        cal.operate()                         # energize + wait for settle
        print(cal.out_actual())               # what is really being sourced
        cal.standby()
"""

from caldmm.drivers import gpib as Gpib  # linux-gpib or VISA, see drivers/gpib.py
try:  # pyserial is only needed for the RS-232C link (pip install 'caldmm[serial]')
    import serial
except ImportError:  # pragma: no cover - exercised only without pyserial
    serial = None

import contextlib
import math
import sys
import time

from dataclasses import dataclass
from typing import Callable, Optional, Protocol, Sequence, runtime_checkable

# Factory-default GPIB address (guide §2.3). There is no remote command to
# change it; it is front-panel only.
DEFAULT_GPIB_ADDRESS = 4
DEFAULT_GPIB_BUS = 0

# Guide §11.5: worst-case ac-voltage settling is 12 s and the recommended
# ``*OPC?``-after-``OUT`` timeout is ">= 45 s". 45 s maps to T-code 15 (100 s).
DEFAULT_TIMEOUT_SEC = 45.0

# RS-232C factory defaults ([OM Table 6-1, p.6-4], guide §2.5): 9600 baud,
# 8 data bits, 1 stop bit, no parity, XON/XOFF flow control.
DEFAULT_SERIAL_BAUD = 9600

# The rates the port can be set to ([OM Table 5-2, p.5-38], the ``SP_SET``
# parameter list). A rate outside this set produces no error at either end —
# only a link that never answers — so callers check against it.
SERIAL_BAUD_RATES = (110, 300, 600, 1200, 2400, 4800, 9600, 19200)

# What the driver appends to an outgoing serial command. The calibrator's
# input EOL terminator is "CR or LF" (guide §2.5 item 2), so one LF is a
# complete terminator; CR LF would be *two*, and the second one delimits an
# empty command. Overridable per session because "or" is the manual's own
# word and only hardware settles which the firmware prefers.
SERIAL_EOL_OUT = "\n"

# Control/C performs the same function as DCL/SDC over serial (guide §2.5
# item 1) — it is the serial spelling of Device Clear.
SERIAL_DCL = "\x03"


# ──────────────────────────────────────────────────────────────────────────
# linux-gpib timeout codes
# ──────────────────────────────────────────────────────────────────────────

# linux-gpib's ``timeout=`` argument is a **T-code enum**, not a number of
# seconds. Code 0 is "no timeout / disabled" and is excluded from the search
# below (it would otherwise match every request).
_TIMEOUT_CODE_SECONDS = {
    1: 10e-6,
    2: 30e-6,
    3: 100e-6,
    4: 300e-6,
    5: 1e-3,
    6: 3e-3,
    7: 10e-3,
    8: 30e-3,
    9: 100e-3,
    10: 300e-3,
    11: 1.0,
    12: 3.0,
    13: 10.0,
    14: 30.0,
    15: 100.0,
    16: 300.0,
    17: 1000.0,
}

TIMEOUT_CODE_NONE = 0        # T-code 0: timeouts disabled
MAX_TIMEOUT_CODE = 17        # T-code 17: 1000 s, the longest linux-gpib offers


def _tcode_for_seconds(seconds: float) -> int:
    """Smallest linux-gpib T-code whose timeout is >= *seconds*.

    Clamped to :data:`MAX_TIMEOUT_CODE` (1000 s). ``0.5 -> 11`` (1 s),
    ``45 -> 15`` (100 s), ``5000 -> 17``.
    """
    if seconds <= 0:
        raise ValueError(
            f"timeout must be > 0 s (T-code 0 disables timeouts and is not "
            f"selectable here), got {seconds!r}")
    for code in sorted(_TIMEOUT_CODE_SECONDS):
        if _TIMEOUT_CODE_SECONDS[code] >= seconds:
            return code
    return MAX_TIMEOUT_CODE


# ──────────────────────────────────────────────────────────────────────────
# Exceptions
# ──────────────────────────────────────────────────────────────────────────

class Fluke5700AError(Exception):
    """Base class for 5700A/5720A driver errors."""


class Fluke5700ACommError(Fluke5700AError):
    """Transport-level failure (dead link, timeout, empty response)."""


class Fluke5700AProtocolError(Fluke5700AError):
    """A response was received but could not be parsed.

    The offending text is kept on :attr:`response` so a caller (and the
    service console) can show the operator what actually came back.
    """

    def __init__(self, message: str, response: Optional[str] = None):
        super().__init__(message)
        self.response = response


class CalibratorFault(Fluke5700AError):
    """One or more entries drained from the calibrator's fault queue.

    :attr:`codes` is a list of ``(code, explanation)`` pairs in queue order —
    earliest first, which guide §8.5 notes is deliberate ("the earliest faults
    are the most likely to point to the problem"). Make programmatic decisions
    on the numeric code only; the text is for logging (§13 item 31 — Appendix A
    contains typos, and ``EXPLAIN?`` echoes them).
    """

    def __init__(self, codes: "list[tuple[int, str]]"):
        self.codes: "list[tuple[int, str]]" = list(codes)
        super().__init__(self._render())

    def _render(self) -> str:
        if not self.codes:
            return "5700A fault (no codes)"
        if len(self.codes) == 1:
            code, text = self.codes[0]
            return f"5700A fault {code}: {text}"
        joined = "; ".join(f"{code}: {text}" for code, text in self.codes)
        return f"5700A faults ({len(self.codes)}): {joined}"

    def __str__(self) -> str:
        return self._render()


class CalibrationSwitchError(CalibratorFault):
    """A command was refused because the rear-panel CALIBRATION switch is set
    to NORMAL.

    A :class:`CalibratorFault` subclass, so ``except CalibratorFault`` still
    catches it, but it carries an actionable message: guide §7.6 / gap G29
    record that the switch position **cannot be queried remotely**, so one of
    the fault codes in :data:`CALIBRATION_SWITCH_FAULT_CODES` is the only way
    a driver ever learns about it.
    """

    def __init__(self, codes: "list[tuple[int, str]]",
                 *, what: Optional[str] = None):
        self.what = what
        super().__init__(codes)

    def _render(self) -> str:
        rendered = "; ".join(f"{code}: {text}" for code, text in self.codes)
        what = self.what or "the command"
        five725 = any(code in (850, 851) for code, _ in self.codes)
        which = ("the **5725A amplifier's own** rear-panel CALIBRATION switch"
                 if five725 else "the rear-panel CALIBRATION switch")
        return (
            f"{what} was refused: {which} is in the NORMAL position "
            f"(faults {rendered}). Set it to ENABLE, repeat the command, and "
            f"return it to NORMAL when finished. There is no remote query for "
            f"the switch position (guide §7.6, gap G29), so this fault is the "
            f"only way to detect it — nothing was stored.")


# ──────────────────────────────────────────────────────────────────────────
# Transport
# ──────────────────────────────────────────────────────────────────────────

@runtime_checkable
class Transport(Protocol):
    """Byte-level link to the calibrator.

    ``serial_poll`` is part of the protocol on purpose: over GPIB a *hardware*
    serial poll also reads **and clears** RQS/SRQ, which ``*STB?`` does not
    (guide §8.2, §9 ``*STB?``). A driver that polls ``*STB?`` will see SRQ
    latched forever. Over RS-232 there is no serial poll and no SRQ at all
    (guide §2.5 item 5), so :class:`SerialTransport` implements it with
    ``*STB?`` — where bit 6 is MSS rather than RQS and so has nothing to
    clear.

    Two members are **optional**, read off the transport with ``getattr``
    rather than declared here, because only one implementation has each:

    ``local()``     return the front panel to local control. GTL over GPIB,
                    the serial-only ``LOCAL`` command over RS-232.
    ``is_serial``   ``True`` on an RS-232 link. The driver reads it to decide
                    whether the session needs the ``REMOTE`` command and the
                    COMP response-style check that GPIB gets from REN and
                    from having no response-style setting at all. Absent means
                    ``False``, so a bare GPIB-shaped fake needs no update.
    ``remote()``    enter Remote. Required *only* on a transport that declares
                    ``is_serial``; GPIB has REN instead, and the ``REMOTE``
                    command is fault 2211 there.

    ``read()`` must raise on a link timeout rather than returning an empty
    string: :func:`read_block` uses that exception as its end-of-block signal
    (see the warning there). It must **not** raise :class:`Fluke5700AError`,
    which that function re-raises instead.
    """

    def write(self, cmd: str) -> None: ...

    def read(self) -> str:
        """One response line, ASCII-decoded and stripped."""

    def query(self, cmd: str) -> str: ...

    def serial_poll(self) -> int: ...

    def set_timeout(self, seconds: float) -> None: ...

    def clear(self) -> None:
        """Device Clear (DCL/SDC) — clears the I/O buffers (guide §3.10)."""

    def close(self) -> None: ...


class GpibTransport:
    """GPIB transport (linux-gpib ``Gpib.Gpib`` or VISA via :mod:`caldmm.drivers.gpib`).

    Constructed eagerly, matching ``hp3458a.py``. Seconds
    are converted to T-codes by :func:`_tcode_for_seconds` — ``Gpib.Gpib``'s
    ``timeout=`` keyword is an enum.
    """

    #: See :class:`Transport`. GPIB gets Remote from REN and has no
    #: response-style setting, so none of the serial session work applies.
    is_serial = False

    def __init__(self, gpib_address: int = DEFAULT_GPIB_ADDRESS,
                 gpib_bus: int = DEFAULT_GPIB_BUS, *,
                 timeout: float = DEFAULT_TIMEOUT_SEC):
        self._dev = Gpib.Gpib(gpib_bus, gpib_address,
                              timeout=_tcode_for_seconds(timeout))

    def write(self, cmd: str) -> None:
        self._dev.write(cmd)

    def read(self) -> str:
        raw = self._dev.read()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("ascii", errors="replace")
        return raw.strip()

    def read_raw(self) -> str:
        """One transport read, **unstripped and not split into lines**.

        See :class:`Transport` on why this exists. linux-gpib returns whatever
        arrived before EOI or before its buffer filled — for a block response
        that is a fixed-size *chunk* with embedded newlines, not a line — so
        :func:`read_block` needs the bytes exactly as they came. :meth:`read`
        strips, which would silently glue the last line of one chunk to the
        first line of the next.
        """
        raw = self._dev.read()
        if isinstance(raw, (bytes, bytearray)):
            raw = raw.decode("ascii", errors="replace")
        return raw

    def query(self, cmd: str) -> str:
        self.write(cmd)
        return self.read()

    def serial_poll(self) -> int:
        return int(self._dev.serial_poll())

    def set_timeout(self, seconds: float) -> None:
        self._dev.timeout(_tcode_for_seconds(seconds))

    def clear(self) -> None:
        self._dev.clear()

    def local(self) -> None:
        """Return the instrument to front-panel control (IEEE-488 GTL).

        The guide's serial-only ``LOCAL`` command has no GPIB spelling; over
        GPIB the GTL bus message is the equivalent. linux-gpib spells it
        ``ibloc``, on the device object in current bindings and on the
        module in older ones — both are tried.
        """
        ibloc = getattr(self._dev, "ibloc", None)
        if ibloc is not None:
            ibloc()
            return
        import gpib
        gpib.ibloc(self._dev.id)

    def close(self) -> None:
        self._dev.close()


class SerialTransport:
    """RS-232C transport (guide §2.5, [OM Chapter 6]).

    The rear-panel serial port is the calibrator's second remote-control
    interface. It is *not* a GPIB emulation, and five things differ; each is
    handled here so the driver above sees one uniform link:

    1. **Device Clear is Control/C** (guide §2.5 item 1). There is no bus
       message to send, so :meth:`clear` writes the byte.
    2. **There is no serial poll and no SRQ** (guide §2.5 item 5).
       :meth:`serial_poll` answers from ``*STB?``; the driver refuses
       ``srq=True`` on this transport rather than pretending.
    3. **There is no REN line.** The calibrator enters Remote on the
       serial-only ``REMOTE`` command, which the driver sends in
       :meth:`Fluke5700A.open`; until it does, every state-changing command
       is silently ignored and logs fault 2213 (guide §7.2).
    4. **The response style is a setting**, defaulting to ``TERM`` — prose
       for a human at a terminal, which no parser can read. ``open()`` puts
       the port into ``COMP`` before it parses anything.
    5. **Framing is by terminator, not EOI**: outgoing commands end with
       :data:`SERIAL_EOL_OUT`, and a response line ends with whatever
       ``SP_SET``'s EOL field selects (``CRLF`` from the factory).

    Flow control defaults to XON/XOFF because the calibrator's own default
    stall method is ``XON`` — it sends XOFF at 80 % of its 128-byte input
    buffer and XON again below 40 % (guide §3.10). A port opened with flow
    control off against a calibrator set to ``XON`` will drop bytes on a long
    write, so the two must agree; ``SP_SET?`` is read at open and the stall
    method it reports is recorded on :attr:`Fluke5700A.serial_setup`.
    """

    #: See :class:`Transport`.
    is_serial = True

    def __init__(self, port: str, baud: int = DEFAULT_SERIAL_BAUD, *,
                 timeout: float = DEFAULT_TIMEOUT_SEC,
                 eol: str = SERIAL_EOL_OUT,
                 xonxoff: bool = True,
                 rtscts: bool = False):
        self._eol = eol
        if serial is None:
            raise ImportError(
                "the 5700A RS-232C link needs pyserial: "
                "pip install 'caldmm[serial]'")
        self._ser = serial.Serial(
            port=port,
            baudrate=baud,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
            timeout=timeout,
            xonxoff=xonxoff,
            rtscts=rtscts,
        )

    def write(self, cmd: str) -> None:
        self._ser.write((cmd + self._eol).encode("ascii"))
        self._ser.flush()

    def read(self) -> str:
        """One response line; raises :class:`TimeoutError` if none arrives.

        A **timeout** and an **empty line** are deliberately different
        outcomes. ``pyserial`` returns ``b""`` only when nothing at all
        arrived, so that is the timeout; a line that is merely blank comes
        back as ``""``, which is what an option-less ``*OPT?`` looks like and
        what :meth:`Fluke5700A._query_raw` is written to accept.

        ``readline`` splits on LF, so a port whose EOL is CR-only would
        return each line one full timeout late. That configuration is
        rejected at :meth:`Fluke5700A.open` rather than tolerated here.
        """
        raw = self._ser.readline()
        if isinstance(raw, (bytes, bytearray)):
            if not raw:
                raise TimeoutError(
                    f"no serial response within {self._ser.timeout} s")
            raw = raw.decode("ascii", errors="replace")
        elif not raw:
            raise TimeoutError(
                f"no serial response within {self._ser.timeout} s")
        return raw.strip()

    def read_raw(self) -> str:
        """One response line **with its terminator**; see :class:`Transport`.

        The serial counterpart of :meth:`GpibTransport.read_raw`. ``readline``
        already frames on LF, so the only difference from :meth:`read` is that
        nothing is stripped — which is what lets :func:`read_block`
        concatenate reads from either transport the same way.
        """
        raw = self._ser.readline()
        if isinstance(raw, (bytes, bytearray)):
            if not raw:
                raise TimeoutError(
                    f"no serial response within {self._ser.timeout} s")
            return raw.decode("ascii", errors="replace")
        if not raw:
            raise TimeoutError(
                f"no serial response within {self._ser.timeout} s")
        return raw

    def query(self, cmd: str) -> str:
        # Drop anything stale before asking: unlike GPIB, where an unread
        # response sits in the instrument's output buffer until it is
        # addressed to talk, serial bytes arrive unbidden and a leftover line
        # would be returned as the answer to the *next* query.
        self._ser.reset_input_buffer()
        self.write(cmd)
        return self.read()

    def serial_poll(self) -> int:
        """The STB, read with ``*STB?`` — there is no serial poll here.

        Nothing is lost by the substitution on this interface: the bit that
        ``*STB?`` cannot clear is RQS, and over serial bit 6 is MSS instead
        (guide §8.2), because there is no SRQ line to latch.
        """
        return int(_unquote(self.query("*STB?")))

    def set_timeout(self, seconds: float) -> None:
        self._ser.timeout = seconds

    def clear(self) -> None:
        """Device Clear — Control/C over serial (guide §2.5 item 1)."""
        self._ser.write(SERIAL_DCL.encode("ascii"))
        self._ser.flush()
        self._ser.reset_input_buffer()

    def local(self) -> None:
        """Return the instrument to front-panel control (``LOCAL``).

        The serial-only text command ([OM Table 5-3, p.5-42]) that does over
        this interface what the GTL bus message does over GPIB.
        """
        self.write("LOCAL")

    def remote(self) -> None:
        """Enter Remote (``REMOTE``), the serial stand-in for asserting REN."""
        self.write("REMOTE")

    def close(self) -> None:
        self._ser.close()


# ──────────────────────────────────────────────────────────────────────────
# Status registers (guide §8)
# ──────────────────────────────────────────────────────────────────────────

# Instrument Status Register — guide §8.4. Bits 14 and 15 are always 0.
ISR_RPTBUSY = 1 << 13
ISR_SETTLED = 1 << 12
ISR_REMOTE = 1 << 11
ISR_WBND = 1 << 10
ISR_SCALE = 1 << 9
ISR_OFFSET = 1 << 8
ISR_PLOCK = 1 << 7
ISR_PSHFT = 1 << 6
ISR_RLOCK = 1 << 5
ISR_RCOMP = 1 << 4
ISR_BOOST = 1 << 3
ISR_EXSENS = 1 << 2
ISR_EXGARD = 1 << 1
ISR_OPER = 1 << 0

_ISR_BITS = (
    (13, "RPTBUSY"),
    (12, "SETTLED"),
    (11, "REMOTE"),
    (10, "WBND"),
    (9, "SCALE"),
    (8, "OFFSET"),
    (7, "PLOCK"),
    (6, "PSHFT"),
    (5, "RLOCK"),
    (4, "RCOMP"),
    (3, "BOOST"),
    (2, "EXSENS"),
    (1, "EXGARD"),
    (0, "OPER"),
)

# Event Status Register — guide §8.3. Bits 1 and 6 are always 0; the upper
# byte is always 0. Cleared on read.
ESR_PON = 1 << 7
ESR_CME = 1 << 5
ESR_EXE = 1 << 4
ESR_DDE = 1 << 3
ESR_QYE = 1 << 2
ESR_OPC = 1 << 0

_ESR_BITS = (
    (7, "PON"),
    (5, "CME"),
    (4, "EXE"),
    (3, "DDE"),
    (2, "QYE"),
    (0, "OPC"),
)

# Serial Poll Status Byte — guide §8.2. Bits 0, 1 and 7 are always 0.
# Bit 6 is RQS after a hardware serial poll and MSS when read with ``*STB?``.
STB_RQS_MSS = 1 << 6
STB_ESB = 1 << 5
STB_MAV = 1 << 4
STB_EAV = 1 << 3
STB_ISCB = 1 << 2

_STB_BITS = (
    (6, "RQS_MSS"),
    (5, "ESB"),
    (4, "MAV"),
    (3, "EAV"),
    (2, "ISCB"),
)


def _decode_bits(value: int, bits, width: int, what: str) -> "dict[str, bool]":
    """Shared body of the ``decode_*`` helpers."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise Fluke5700AProtocolError(
            f"{what} value must be an int, got {value!r}", str(value))
    if not 0 <= value < (1 << width):
        raise Fluke5700AProtocolError(
            f"{what} value {value} out of range for a {width}-bit register",
            str(value))
    return {name: bool(value & (1 << bit)) for bit, name in bits}


def decode_isr(value: int) -> "dict[str, bool]":
    """Decode an ``ISR?`` value into named flags (guide §8.4).

    ``ISR?`` is **non-destructive** — guide §13 item 17 resolves the OM's
    contradictory "returns and clears" wording in favour of the register map;
    use ``ISCR?`` when latched change detection is wanted.
    """
    return _decode_bits(value, _ISR_BITS, 16, "ISR")


def decode_esr(value: int) -> "dict[str, bool]":
    """Decode an ``*ESR?`` value into named flags (guide §8.3).

    Reading the ESR clears it, so a poller that only tests OPC silently
    discards CME/EXE/DDE — evaluate the whole returned byte (gap G-§19.8 item 3).
    """
    return _decode_bits(value, _ESR_BITS, 8, "ESR")


def decode_stb(value: int) -> "dict[str, bool]":
    """Decode a status byte into named flags (guide §8.2).

    Accepts either a hardware serial-poll result or an ``*STB?`` response;
    bit 6 is reported as ``RQS_MSS`` because its meaning depends on which.
    """
    return _decode_bits(value, _STB_BITS, 8, "STB")


# ──────────────────────────────────────────────────────────────────────────
# Long-term state (guide §9 ``STATE?``)
# ──────────────────────────────────────────────────────────────────────────

STATE_NAMES = {
    0: "Operating",
    1: "Self diagnostics",
    2: "Self diagnostics halted by a fault",
    3: "Calibration check",
    4: "DC zeros calibration",
    5: "Calibration, +dc reference",
    6: "Calibration, -dc reference",
    7: "Calibration, 1 Ohm reference",
    8: "Calibration, 10 kOhm reference",
    9: "Calibration, internal adjustment",
    10: "Calibration, wideband positive gain",
    11: "Calibration, wideband negative gain",
    12: "Wideband flatness calibration",
    13: "Range calibration",
    14: "5725A voltage diagnostics",
    15: "5725A current diagnostics",
    16: "5725A voltage troubleshooting",
    17: "5725A current troubleshooting",
    18: "Analog output tripped to dormant",
    19: "Starting up",
}

#: The trip indicator. Guide §11.7: several conditions revert the calibrator
#: to standby by itself; a driver must not assume the output stayed on.
STATE_TRIPPED = 18

#: "Self diagnostics halted by a fault". With ``DIAGFLT HALT`` selected, a
#: diagnostic that hits a fault **stops here and waits** for ``DIAG CONT`` or
#: ``DIAG ABORT`` (guide §9 ``DIAGFLT``). No ``*OPC`` arrives while it waits,
#: so :meth:`Fluke5700A._run_long_term` treats this state as an exit
#: condition — that is what makes fault-to-fault stepping possible.
STATE_DIAG_HALTED = 2

#: Not a calibrator state: the value :meth:`Fluke5700A._run_long_term` reports
#: to a progress callback when the ``STATE?`` poll itself failed. Distinct from
#: every real code in :data:`STATE_NAMES`, so it can never match a
#: ``stop_states`` entry.
STATE_UNKNOWN = -1


# ──────────────────────────────────────────────────────────────────────────
# Pure response parsers
# ──────────────────────────────────────────────────────────────────────────

def _unquote(text: str) -> str:
    """Strip one layer of surrounding double quotes, if present.

    Guide gap G14: the §4.2 list of quoted responses is demonstrably not
    exhaustive and ``EXPLAIN?`` is documented both ways (§13 item 31), so
    every response is unquoted defensively.
    """
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        return text[1:-1]
    return text


def _to_float(field: str, resp: str, what: str) -> float:
    try:
        return float(field)
    except (TypeError, ValueError) as exc:
        raise Fluke5700AProtocolError(
            f"{what}: {field!r} is not a number in {resp!r}", resp) from exc


def _to_int(field: str, resp: str, what: str) -> int:
    """Parse an integer field, tolerating exponential notation.

    Guide §4.1 types several responses ``(Integer)`` but the calibrator emits
    floats freely; ``365`` and ``3.65E+02`` must both work.
    """
    try:
        return int(field)
    except (TypeError, ValueError):
        pass
    try:
        value = float(field)
    except (TypeError, ValueError) as exc:
        raise Fluke5700AProtocolError(
            f"{what}: {field!r} is not an integer in {resp!r}", resp) from exc
    if value != int(value):
        raise Fluke5700AProtocolError(
            f"{what}: {field!r} is not an integer in {resp!r}", resp)
    return int(value)


def _fields(resp: str, count: int, what: str) -> "list[str]":
    text = _unquote(resp)
    if not text:
        raise Fluke5700AProtocolError(f"{what}: empty response", resp)
    parts = [f.strip() for f in text.split(",")]
    if len(parts) != count:
        raise Fluke5700AProtocolError(
            f"{what}: expected {count} comma-separated fields, got "
            f"{len(parts)} in {resp!r}", resp)
    return parts


def parse_out(resp: str) -> "tuple[float, str, float]":
    """Parse an ``OUT?`` response into ``(amplitude, unit, frequency_hz)``.

    Response form (guide §9 ``OUT?``): ``(Float) amplitude, (String) unit,
    (Float) frequency`` — e.g. ``-1.520000E+01,V,0.000000E+00``. Units seen
    are ``V``, ``A``, ``OHM`` and ``DBM``; the frequency field is ``0`` for dc
    and ohms and may be a bare ``0`` or exponential (guide §4.1).

    ``OUT?`` never contains multipliers (guide §11.4 item 3) — the amplitude is
    always in base units. In the resistance function it returns the
    *characterized* value actually being sourced, not the nominal request
    (§11.4 item 2); use it, not the requested value, as the error reference.
    """
    amp, unit, freq = _fields(resp, 3, "OUT?")
    return (_to_float(amp, resp, "OUT? amplitude"),
            _unquote(unit).upper(),
            _to_float(freq, resp, "OUT? frequency"))


def parse_uncert(resp: str) -> "tuple[Optional[float], str, int]":
    """Parse an ``UNCERT?`` response into ``(uncertainty, units, days)``.

    Guide §9 ``UNCERT?``: the value is ``-1.0`` when **no specification is
    available** for the present output — that sentinel is returned as ``None``
    so it can never be mistaken for a real (negative) uncertainty.

    The unit string is left as the instrument sent it, uppercased. It is
    open-ended — the manual writes "``PPM``, ``PCT``, ``V``, ``A``, ``OHM``,
    etc." (gap G22) — so it is deliberately not validated against an enum.
    """
    value, units, days = _fields(resp, 3, "UNCERT?")
    uncertainty = _to_float(value, resp, "UNCERT? value")
    return (None if uncertainty == -1.0 else uncertainty,
            _unquote(units).upper(),
            _to_int(days, resp, "UNCERT? interval"))


def parse_state(resp: str) -> "tuple[int, str]":
    """Parse a ``STATE?`` response into ``(code, substate)``.

    Guide §9 ``STATE?``: element 1 is the gross state (see
    :data:`STATE_NAMES`), element 2 a free-text description of the activity
    underway. The substate strings are never enumerated by the manuals
    (gap G21) — **log them, do not branch on them**; branch on the code, and
    watch for :data:`STATE_TRIPPED`.

    The substate is absent for some states, so a single-field response is
    accepted and yields an empty substate.
    """
    text = _unquote(resp)
    if not text:
        raise Fluke5700AProtocolError("STATE?: empty response", resp)
    head, _, tail = text.partition(",")
    code = _to_int(head.strip(), resp, "STATE? code")
    return code, _unquote(tail.strip())


def parse_opt(resp: str) -> "list[str]":
    """Parse an ``*OPT?`` response into a list of installed-option strings.

    Guide §9 ``*OPT?``: "a series of quoted strings separated by commas", e.g.
    ``"5725A Attached","5215A Attached"``. There is no complete list of option
    strings and the *empty* case (no options, no amplifiers) is undefined
    (gap G23), so an empty response yields ``[]`` rather than an error.
    """
    text = resp.strip()
    if not text:
        return []
    # Unquote per field, not the whole response: the fields carry their own
    # quotes, so stripping the outer pair first would corrupt them.
    return [_unquote(part) for part in text.split(",") if _unquote(part)]


@dataclass(frozen=True)
class Identity:
    """Parsed ``*IDN?`` response (guide §9 ``*IDN?``)."""

    raw: str
    manufacturer: str
    model: str
    serial: str
    firmware: str

    @property
    def firmware_fields(self) -> "list[str]":
        """The ``Main+Inguard+Boost`` firmware letters, split on ``+``."""
        return self.firmware.split("+")

    @property
    def has_amplifier(self) -> bool:
        """True when an auxiliary amplifier reports a firmware version.

        ``*`` in the third (boost) position means no amplifier is attached.
        Note guide gap G30: ``*IDN?`` cannot distinguish Series I from
        Series II, and the firmware-letter format is unspecified — feature
        detect rather than version-compare.
        """
        fields = self.firmware_fields
        return len(fields) >= 3 and fields[2].strip() not in ("", "*")


def parse_idn(resp: str) -> Identity:
    """Parse ``*IDN?`` into an :class:`Identity`.

    Form: ``FLUKE,5700A|5720A,<serial>,<Main>+<Inguard>+<Boost or *>`` — real
    example ``FLUKE,5720A,5248000,1.0+B+*``. The OM types field 3 as ``0``,
    which guide §13 item 19 records as a typo for ``string``.
    """
    fields = _fields(resp, 4, "*IDN?")
    return Identity(raw=resp.strip(), manufacturer=_unquote(fields[0]),
                    model=_unquote(fields[1]), serial=_unquote(fields[2]),
                    firmware=_unquote(fields[3]))


@dataclass(frozen=True)
class SerialSetup:
    """Parsed ``SP_SET?`` response — the RS-232C port's configuration."""

    raw: str
    baud: int
    style: str          # TERM | COMP
    stall: str          # XON | RTS | NOSTALL
    data_bits: str      # DBIT7 | DBIT8
    stop_bits: str      # SBIT1 | SBIT2
    parity: str         # PNONE | PEVEN | PODD | PIGNORE
    eol: str            # CR | LF | CRLF

    @property
    def parseable(self) -> bool:
        """True when responses are machine-readable, i.e. ``COMP`` style.

        ``TERM`` "responses contain more descriptive text intended for an
        operator using a terminal interactively" ([OM §5-13, p.5-10]) — a
        driver must never parse them.
        """
        return self.style == "COMP"

    @property
    def readable_lines(self) -> bool:
        """True when response lines end in something ``readline`` can see.

        ``readline`` splits on LF, so ``CR``-only framing leaves every read
        waiting for the full timeout before returning its partial line.
        """
        return "LF" in self.eol


def parse_sp_set(resp: str) -> SerialSetup:
    """Parse ``SP_SET?`` into a :class:`SerialSetup` (guide §9 ``SP_SET?``).

    Seven comma-separated fields in a fixed order, e.g. the factory default
    ``9600,TERM,XON,DBIT8,SBIT1,PNONE,CRLF`` ([OM Table 5-2, p.5-39]).

    A response that does not have this shape is nearly always the *same*
    diagnosis — the port is still in ``TERM`` style, where the answer is
    prose — so the error says so rather than reporting a bare field count.
    """
    fields = [field.strip().upper()
              for field in _unquote(resp.strip()).split(",")]
    if len(fields) != 7:
        raise Fluke5700AProtocolError(
            f"SP_SET? answered {resp!r}, which is not the seven fields of "
            f"'9600,COMP,XON,DBIT8,SBIT1,PNONE,CRLF'. The usual cause is a "
            f"port still in TERM response style, whose replies are prose "
            f"meant for a human ([OM §5-13]).", resp)
    return SerialSetup(raw=resp.strip(),
                       baud=_to_int(fields[0], resp, "SP_SET? baud"),
                       style=fields[1], stall=fields[2], data_bits=fields[3],
                       stop_bits=fields[4], parity=fields[5], eol=fields[6])


# ──────────────────────────────────────────────────────────────────────────
# Multi-line block responses (guide §4.2; gap G1)
# ──────────────────────────────────────────────────────────────────────────

def is_block_end(line: str) -> bool:
    """Default stop predicate: a line whose content starts with ``"``."""
    return line.lstrip().startswith('"')


#: Per-line read timeout used while collecting a block response. Short on
#: purpose — it is the *only* end-of-block signal if EOI is asserted once per
#: block rather than once per line.
DEFAULT_BLOCK_LINE_TIMEOUT_SEC = 2.0

#: Sanity bound on block length. ``CAL_CLST?`` returns roughly 400 name/value
#: pairs (gap G2), so this is generous.
DEFAULT_BLOCK_MAX_LINES = 4096

#: Bytes per line allowed for when the *byte* budget of a block is checked.
#: Report lines are about 80 characters; this is deliberately loose, because
#: it only backstops a block that never sends its closing delimiter.
MAX_BLOCK_LINE_CHARS = 256


def _block_is_complete(text: str) -> bool:
    """True once the closing delimiter of a quoted block has arrived.

    Guide §4.2: the block is delimited by a lone ``"`` at the start and at the
    end. Anything after the first line that is *only* a quote is therefore the
    closing delimiter. Strict equality on purpose — the default
    :func:`is_block_end` predicate is looser, and a content line that merely
    began with a quote must not end the read early.
    """
    for line in text.split("\n")[1:]:
        if line.strip() == '"':
            return True
    return False


def read_block(transport: Transport, *,
               line_timeout: float = DEFAULT_BLOCK_LINE_TIMEOUT_SEC,
               is_end: Callable[[str], bool] = is_block_end,
               restore_timeout: Optional[float] = None,
               max_lines: int = DEFAULT_BLOCK_MAX_LINES,
               skip_opening_quote: bool = True) -> "list[str]":
    """Read a multi-line quoted-block response line by line.

    Used by ``CAL_SHIFT?`` / ``CAL_CLST?`` / ``CAL_SLST?`` / ``CAL_RPT?``.
    Call it after *writing* the query; it consumes the response lines.
    :meth:`Fluke5700A.read_block` is the convenient form — it writes the query
    and restores the session timeout for you.

    .. warning::
       **This mutates the transport timeout.** It sets *line_timeout*
       (2 s by default) before reading and only restores a timeout afterwards
       if *restore_timeout* is given. Called bare, it therefore leaves the
       transport running at 2 s, and the next slow operation on that session —
       an ``*OPC?`` after a long-term ``CAL_ADJ``, say — fails as a spurious
       :class:`Fluke5700ACommError`. Always pass *restore_timeout*, or use
       :meth:`Fluke5700A.read_block`, which passes the session timeout.

    **This was documented gap G1, and hardware has now settled it** — against
    a 5700A (firmware 1.6) on linux-gpib, 2026-08-14:

    * **EOI is asserted once, at the end of the whole block** — not per line.
      Each read returns a fixed-size *chunk* (512 bytes on this stack) with
      embedded newlines, and the final short read carries EOI.
    * The block really is delimited by a **lone ``"`` on its own line** at both
      ends, exactly as guide §4.2 says, so ``skip_opening_quote`` is right.

    Neither of the two framings the guide considered was what happened, and
    the per-line loop originally written here failed in the worst way: the
    opening ``"`` arrives glued to the first chunk, the end predicate matched
    it immediately, and ``CAL_RPT?`` returned an **empty block** rather than an
    error. So reads are concatenated and split into lines afterwards.

    ``line_timeout`` remains the between-chunk timeout and the backstop for a
    block that never sends its closing delimiter.

    Failure modes, all distinguishable from a complete block:

    * timeout with **zero** bytes collected — a communication failure, not an
      empty block: :class:`Fluke5700ACommError`;
    * more than *max_lines* lines — a truncated block must never be mistaken
      for a complete one: :class:`Fluke5700AProtocolError`;
    * a :class:`Fluke5700AError` raised by *transport* is **re-raised**, not
      treated as end-of-block. Pass the raw transport, therefore, not a reader
      that wraps its timeouts into driver exceptions — such a wrapper would
      turn every normal end-of-block into an error.
    """
    # A transport that offers read_raw() is read as a *byte stream*: real
    # hardware hands back fixed-size chunks with embedded newlines, so the
    # reads are concatenated and split into lines afterwards. One that offers
    # only the line-oriented read() (the test fakes, and any transport that
    # frames for itself) has its terminator put back, which makes both paths
    # reconstruct the identical text.
    raw_read = getattr(transport, "read_raw", None)
    if raw_read is None:
        def raw_read() -> str:
            return transport.read() + "\n"

    text = ""
    completed = False
    transport.set_timeout(line_timeout)
    try:
        while True:
            try:
                chunk = raw_read()
            except Fluke5700AError:
                raise
            except Exception as exc:
                # linux-gpib signals a timeout by raising ``gpib.GpibError``,
                # which derives from neither OSError nor TimeoutError, so the
                # catch has to be broad here.
                if text:
                    break                 # timeout after data: block done
                raise Fluke5700ACommError(
                    f"no block response within {line_timeout} s "
                    f"(0 bytes received)") from exc
            text += chunk
            if _block_is_complete(text):
                completed = True
                break
            if len(text) > max_lines * MAX_BLOCK_LINE_CHARS:
                raise Fluke5700AProtocolError(
                    f"block response exceeded max_lines={max_lines} with no "
                    f"closing quote line; refusing to return a truncated "
                    f"block", text[-200:])
    finally:
        if restore_timeout is not None:
            transport.set_timeout(restore_timeout)

    parts = text.split("\n")
    if parts and parts[-1] == "":
        parts.pop()                       # artifact of the block's last LF

    lines: "list[str]" = []
    for index, line in enumerate(parts):
        line = line.rstrip("\r")
        if index == 0 and skip_opening_quote and line.strip() == '"':
            continue                      # opening delimiter, guide §4.2
        if is_end(line):
            return lines
        if len(lines) >= max_lines:
            raise Fluke5700AProtocolError(
                f"block response exceeded max_lines={max_lines} with no "
                f"closing quote line; refusing to return a truncated block",
                line)
        lines.append(line)

    # Nothing but the opening delimiter, and the link then went quiet: that is
    # a dead read, not an empty block. A block that ended properly on its
    # closing delimiter is allowed to be empty.
    if not completed and not lines:
        raise Fluke5700ACommError(
            f"block response ended after {len(text)} byte(s) with no content "
            f"and no closing quote line (timeout at {line_timeout} s)")
    return lines


# ──────────────────────────────────────────────────────────────────────────
# Offline fault-code table (guide §12.1)
# ──────────────────────────────────────────────────────────────────────────

#: Transcribed verbatim from guide §12.1 ([OM Appendix A]), **including its
#: typos** — guide §13 item 31: match on the numeric code, never on the text
#: (e.g. 831 "2-ire", 2222 "Invlaid", and 2202/2229 sharing a description).
#:
#: This is a subset of the 692-code table: the 200-level trip codes, the whole
#: 800-level (executive / envelope / CALIBRATION-switch faults), the 1300-level
#: output monitor, the 1800-level normal-output operations, the 2000-level
#: analog operations manager, the whole 2200-level (remote interface) and the
#: whole 3100-level (high-voltage self-diagnostics). The 3100 range is the one
#: that matters for a unit with a suspect **A14 High Voltage Control PCA**.
#:
#: An offline table is required because faults must be decodable with no
#: instrument attached, and because a sick unit's own ``EXPLAIN?`` may
#: misbehave. :meth:`Fluke5700A.drain_faults` prefers ``EXPLAIN?`` and falls
#: back to this table.
FAULT_CODES = {
    # 200-Level — 5725 Boost System (trip codes only; guide §11.7)
    210: "Output Tripped To Standby",
    222: "Output Tripped To Standby",
    224: "Output Tripped To Standby",
    227: "Output Tripped To Standby",
    235: "Output Tripped To Standby",

    # 800-Level — Executive Subsystem
    800: "Wideband Module Needed",
    801: "Amplifier Not Selected Or Connected",
    802: "Amplifier Must Remain On For This Output",
    803: "Over Limits Of Locked Range",
    804: "Under Limits Of Locked Range",
    806: "Invalid Date",
    807: "Invalid Time",
    808: "DC dBm Not Allowed",
    809: "Ext Ref Value Out Of Range",
    810: "Bad Edit Digit Movement",
    811: "Not Wideband Units",
    812: "Cannot Set Frequency With Ohms",
    813: "Bad Units",
    814: "Wrong Polarity For Limit",
    815: "Outside Entry Limits",
    816: "Magnitude Too Large For Calibrator",
    817: "Magnitude Too Small For Calibrator",
    818: "Frequency Too Large For Calibrator",
    819: "Frequency Too Small For Calibrator",
    820: "Calibrator Cannot Source That Value",
    821: "V Limit Outside Calibrator Ability",
    822: "I Limit Outside Calibrator Ability",
    823: "Cannot Adjust Frequency To <<= 0 Hz",
    824: "Offset Not Allowed Now",
    825: "Scale Not Allowed Now",
    826: "Ohms Reading Set Too High",
    827: "Ohms Reading Set Too Low",
    828: "Cannot Use External Sense Now",
    829: "Cannot Use Phase Shift Now",
    830: "Cannot Use Phase Lock Now",
    831: "Cannot Use 2-ire Comp Now",
    832: "Could not Do Default Wideband Output",
    833: "Bad Selector For Set/Get Item",
    834: "Cannot Boost This Output Mode",
    835: "Cannot Use Ext Sense On Selected Range",
    836: "Cannot Use 2 Wire Comp On Selected Range",
    837: "Cannot Lock This Range",
    838: "Cannot Set Clock, Rear Switch In NORMAL",
    839: "Cannot Fmt EEPROM, Rear Switch In NORMAL",
    840: "Illegal Output Post For Current",
    841: "No 5725 Available For Selected Output",
    842: "Not A Valid Wideband Flatness Point",
    843: "Wideband Flatness Cal Is Not Active",
    844: "Report String Too Long",
    845: "Cannot Store, Rear Switch In NORMAL",
    846: "Range Calibration Is Not Active",
    847: "Magnitude Too High For Range Cal",
    848: "Magnitude Too Low For Range Cal",
    849: "Invalid Calibrator Setup Block",
    850: "Cannot Format, 5725 Switch In NORMAL",
    851: "Cannot Store, 5725 Switch In NORMAL",
    852: "Calibration Steps Out Of Order",
    853: "Cannot Use External Guard Now",
    854: "Cannot Use Ext Guard On Selected Range",
    855: "Cannot Set String, Rear Switch in NORMAL",
    856: "Present Output Exceeds Selected Limit",
    857: "Bad Selector For Reference Calibration",
    858: "Cannot Change Range Now",
    859: "Hardware Not Installed For This Range",
    860: "Cannot Use Amplifier For This Output",
    861: "5725 Cannot Ext Sense At That Frequency",

    # 1300-Level — Output Monitor Subsystem
    1300: "Undefined Signal",
    1301: "Output Tripped To Standby",
    1302: "Output Tripped To Standby",
    1303: "DC Zero Calibration is Needed (Every 30 Days)",

    # 1800-Level — Normal Output Operations Subsystem
    1800: "DORMANT To OPERATE",
    1801: "Bad Transition Type",
    1802: "Bad Boolean Selector",
    1803: "NRMrngStby Encountered Error",
    1804: "DCV Called For Non-DCV Range",
    1805: "ACV Called For Non-ACV Range",
    1806: "Bad ACV Frequency Range",
    1807: "Hi Res Frequency Too High",
    1808: "ACV Amplitude Correction Failure",
    1809: "Cannot Phase Lock To Signal",
    1810: "Bad Phase Quadrant",
    1811: "Current Called for Non-Current Range",
    1812: "Ohms Called for Non-Ohms Range",
    1813: "Cannot Phase Lock, Output Not AC",
    1814: "Cannot Phase Shift, Output Not AC",
    1815: "Bad Wide Band Range",
    1816: "Freq. Too High Even For Wide Band",
    1817: "Illegal Current Output Location",
    1818: "Output Current Out Of Tolerance",
    1819: "Current Compliance Voltage Exceeded",
    1820: "2-Wire Compensation Current Exceeded",
    1821: "NRMbstcur Passed Bad Range",
    1822: "5220 No Longer Connected",
    1823: "5220 Detected Fault And Shut Off",
    1824: "5205 No Longer Connected",
    1825: "5205 Detected Fault And Shut Off",
    1826: "5725 No Longer Connected",
    1827: "14 Bit DAC Scaling Found Zero Output",

    # 2000-Level — Analog Operations Manager Subsystem
    2000: "Bad Command Code",
    2001: "Bad Signal",
    2002: "Long Term Command In Progress",
    2003: "Guard Crossing Protocol Failed To Start",
    2004: "Fatal Fault, Output Tripped",
    2005: "Tripped While Calibrating or Diagnosing",

    # 2200-Level — Remote Interface Subsystem
    2200: "Unknown Command",
    2201: "Invalid Number Of Parameters",
    2202: "Invalid Cal Constant Name",
    2203: "Invalid Keyword",
    2204: "Invalid Range",
    2205: "Invalid Parameter Type",
    2206: "Invalid Parameter Unit",
    2207: "Invalid Parameter Value",
    2208: "IEEE488.2 I/O DEADLOCK",
    2209: "Spare",
    2210: "IEEE488.2 INTERRUPTED Query",
    2211: "Not Allowed From GPIB Interface",
    2212: "Not Allowed From Serial Interface",
    2213: "Remote Only",
    2214: "Invalid Syntax",
    2215: "IEEE488.2 UNTERMINATED Command",
    2216: "Symbol Table Overflow",
    2217: "Command Reserved For Service",
    2218: "Invalid Binary Number",
    2219: "Invalid Binary Block",
    2220: "Invalid Character",
    2221: "Invalid Decimal Number",
    2222: "Invlaid Hexadecimal Block",
    2223: "Invalid Hexadecimal Number",
    2224: "Too Many Parameters",
    2225: "Invalid Octal Number",
    2226: "Too Many Characters",
    2227: "Invalid String",
    2228: "Invalid Register Address",
    2229: "Invalid Cal Constant Name",
    2230: "Remote Serial Port Dead",
    2231: "IEEE488.2 Query After Indef Response",

    # 3100-Level — Self Diagnostics Subsystem (High Voltage).
    # Codes 3121 and 3134 are absent from [OM Appendix A] and so from here.
    3100: "A14: 8255 Control Word",
    3101: "A14: 8255 Port A Fault",
    3102: "A14: 8255 Port B Fault",
    3103: "A14: 8255 Port C Fault",
    3104: "A15: HV Oven Regulation Fault",
    3105: "A15: HV I Oven Regulation Fault",
    3106: "A15: DC HV Amp Noise Fault",
    3107: "A15: DC HV Amp Offset Fault",
    3108: "A15: DC HV Amp Gain Fault",
    3109: "A15: HV +DC Preamplifier Fault",
    3110: "A15: HV +DC Series Pass & Current Fault",
    3111: "A15: HV +DC High Voltage Output Fault",
    3112: "A15: HV +DC Ref/Error Amplitude Fault",
    3113: "A15: HV -DC Preamplifier Fault",
    3114: "A15: HV -DC Series Pass & Current Fault",
    3115: "A15: HV -DC High Voltage output Fault",
    3116: "A15: HV -DC Reference/Error Amp Fault",
    3117: "A14/A15: HV +DC Current Error Amp Fault",
    3118: "A14/A15: HV -DC Current Error Amp Fault",
    3119: "A14/A15: HV +DC Current Abs. Value",
    3120: "A14/A15: HV -DC Current Abs. Value",
    3122: "A14/A15: HV AC 1KHZ, Preamp Fault (lo)",
    3123: "A14/A15: HV AC 1KHZ, Preamp Fault (mid)",
    3124: "A14/A15: HV AC 1KHZ, Preamp Fault (hi)",
    3125: "A14/A15: HV AC 100HZ, Preamp Fault (lo)",
    3126: "A14/A15: HV AC 100HZ, Preamp Fault",
    3127: "A14/A15: HV AC 100HZ, Preamp Fault (hi)",
    3128: "A14/A15: HV AC 1KHZ, Output Fault (lo)",
    3129: "A14/A15: HV AC 1KHZ, Output Fault (mid)",
    3130: "A14/A15: HV AC 1KHZ, Output Fault (hi)",
    3131: "A14/A15: HV AC 100HZ, Output Fault (lo)",
    3132: "A14/A15: HV AC 100HZ, Output Fault (mid)",
    3133: "A14/A15: HV AC 100HZ, Output Fault (hi)",
    3135: "Assembly A14 Not Responding",
    3136: "A14: 8255 Was Reset",
    3137: "A14/A15/A16: 2.2A AC Range Mag. Fault",
    3138: "A14/A15/A16: 2.2A AC Range Compliance",
    3139: "A14/A15/A16: 2.2A AC Range Amplitude",
    3140: "A14/A15/A16: 2.2A AC Range Abs. Value",
    3141: "A14/A15/A16: 2.2A DC Range Dummy Load",
    3142: "A14/A15/A16: 2.2A DC Range Compliance",
    3143: "A14/A15/A16: 2.2A DC Range Magnitude",

    # 4300-Level — Self Test Subsystem. These are the codes ``*TST?`` logs
    # into the fault queue when it answers 1 (guide §9 ``*TST?``, §12.1), so
    # :meth:`Fluke5700A.self_test` needs them decodable offline.
    4301: "ROM Checksum",
    4302: "RAM",
    4303: "Real Time Clock",
    4304: "Keyboard",
    4305: "Output Display",
    4306: "Control Display",
    4307: "IEEE488",
    4308: "Rear Panel DUART",
    4309: "Guard Crossing DUART",
    4310: "Interrupt Hardware",
    4311: "Inguard CPU ROM Fault",
    4312: "Inguard CPU RAM Fault",
    4313: "Front Panel Refresh Fault",
}

#: The **complete** 692-code table from the Series II Service Manual,
#: chapter 4 "Fault Codes" ([SM pp.4-15..4-37]; transcription of record in
#: ``docs/5700a/fluke5700a-fault-codes.md``). Merged *under* :data:`FAULT_CODES`
#: below: where both sources carry a code, the guide-section-12.1 wording
#: above wins. The two differ on exactly three codes — 823 ``<<= 0 Hz``,
#: 831 ``2-ire``, 2222 ``Invlaid`` — where the guide preserves the OM
#: print's artifacts verbatim (the printed tables look like dumps of the
#: firmware's own strings, so the "typo" may be what ``EXPLAIN?`` really
#: says) and this table carries the corrected spelling. The console's
#: ``explain`` shows offline and live text side by side, so any wording
#: drift against real firmware is visible, never silent.
_SERVICE_MANUAL_FAULTS = {
    # 100-Level Faults: Air Flow Monitoring Subsystem
    100: "Low Air Flow",
    # 200-Level Faults: 5725 Boost System
    200: "5725 No Error",
    201: "5725 Self-Test ROM Failure",
    202: "5725 Self-Test RAM Failure",
    203: "5725 Self-Test EEPROM Failure",
    204: "5725 Self-Test Data Bus Failure",
    205: "5725 Self-Test CLAMPS Circuit Failure",
    206: "5725 Self-Test HVCLR Circuit Failure",
    207: "5725 Self-Test DAC Failure",
    208: "5725 Self-Test Watchdog Timer Failure",
    209: "5725 Current Heatsink Too Hot",
    210: "Output Tripped To Standby",
    211: "5725 Current Compliance Voltage Too High",
    212: "5725 Current Compliance Voltage Too High",
    213: "5725 +400V Supply Did Not Shut Off",
    214: "5725 -400V Supply Did Not Shut Off",
    215: "5725 Voltage Heatsink Too Hot",
    216: "5725 Voltage Heatsink Too Hot",
    217: "5725 +400V Supply Too Small",
    218: "5725 +400V Supply Too Large",
    219: "5725 -400V Supply Too Large",
    220: "5725 -400V Supply Too Small",
    221: "5725 +400V Supply Current Too High",
    222: "Output Tripped To Standby",
    223: "5725 -400V Supply Current Too High",
    224: "Output Tripped To Standby",
    225: "5725 Fan Not Working",
    226: "5725 CLAMPS Fault",
    227: "Output Tripped To Standby",
    228: "5725 Software TRAP",
    229: "5725 Cable Was Off",
    230: "5725 RESET (power-up or watchdog timer)",
    231: "5725 Guard-Crossing Timeout",
    232: "5725 Illegal/Unexecutable Command",
    233: "5725 Non-Maskable Interrupt Occurred",
    234: "5725 HVCLEAR Circuit Activated",
    235: "Output Tripped To Standby",
    # 400-Level Faults: Calibration Constant Subsystem
    400: "Bad Cal Constant ID",
    # 500-Level Faults: Configuration Finder Subsystem
    500: "Bad Mode To CNFmodeRanges",
    501: "Configuration Table Overflow",
    502: "Bad Amplifier Type Selection",
    503: "Bad Flat Constant Type Selection",
    504: "Bad Flat Constant Type In Table",
    505: "5725 Guard Crossing Wouldn't Start",
    # 600-Level Faults: Dot Matrix Display Driver Subsystem
    600: "Bad Row Selector",
    601: "Bad Column Argument",
    602: "Bad Character",
    # 800-Level Faults: Executive Subsystem
    800: "Wideband Module Needed",
    801: "Amplifier Not Selected Or Connected",
    802: "Amplifier Must Remain On For This Output",
    803: "Over Limits Of Locked Range",
    804: "Under Limits Of Locked Range",
    806: "Invalid Date",
    807: "Invalid Time",
    808: "DC dBm Not Allowed",
    809: "Ext Ref Value Out Of Range",
    810: "Bad Edit Digit Movement",
    811: "Not Wideband Units",
    812: "Cannot Set Frequency With Ohms",
    813: "Bad Units",
    814: "Wrong Polarity For Limit",
    815: "Outside Entry Limits",
    816: "Magnitude Too Large For Calibrator",
    817: "Magnitude Too Small For Calibrator",
    818: "Frequency Too Large For Calibrator",
    819: "Frequency Too Small For Calibrator",
    820: "Calibrator Cannot Source That Value",
    821: "V Limit Outside Calibrator Ability",
    822: "I Limit Outside Calibrator Ability",
    823: "Cannot Adjust Frequency To <= 0 Hz",
    824: "Offset Not Allowed Now",
    825: "Scale Not Allowed Now",
    826: "Ohms Reading Set Too High",
    827: "Ohms Reading Set Too Low",
    828: "Cannot Use External Sense Now",
    829: "Cannot Use Phase Shift Now",
    830: "Cannot Use Phase Lock Now",
    831: "Cannot Use 2-Wire Comp Now",
    832: "Could not Do Default Wideband Output",
    833: "Bad Selector For Set/Get Item",
    834: "Cannot Boost This Output Mode",
    835: "Cannot Use Ext Sense On Selected Range",
    836: "Cannot Use 2 Wire Comp On Selected Range",
    837: "Cannot Lock This Range",
    838: "Cannot Set Clock, Rear Switch In NORMAL",
    839: "Cannot Fmt EEPROM, Rear Switch In NORMAL",
    840: "Illegal Output Post For Current",
    841: "No 5725 Available For Selected Output",
    842: "Not A Valid Wideband Flatness Point",
    843: "Wideband Flatness Cal Is Not Active",
    844: "Report String Too Long",
    845: "Cannot Store, Rear Switch In NORMAL",
    846: "Range Calibration Is Not Active",
    847: "Magnitude Too High For Range Cal",
    848: "Magnitude Too Low For Range Cal",
    849: "Invalid Calibrator Setup Block",
    850: "Cannot Format, 5725 Switch In NORMAL",
    851: "Cannot Store, 5725 Switch In NORMAL",
    852: "Calibration Steps Out Of Order",
    853: "Cannot Use External Guard Now",
    854: "Cannot Use Ext Guard On Selected Range",
    855: "Cannot Set String, Rear Switch in NORMAL",
    856: "Present Output Exceeds Selected Limit",
    857: "Bad Selector For Reference Calibration",
    858: "Cannot Change Range Now",
    859: "Hardware Not Installed For This Range",
    860: "Cannot Use Amplifier For This Output",
    861: "5725 Cannot Ext Sense At That Frequency",
    # 900-Level Faults: Keyboard Driver Subsystem
    900: "Keyboard Output Queue Full",
    # 1000-Level Faults: 5725 Communication Receive Subsystem
    1000: "Could not ACK Packet From 5725",
    1001: "Illegal 5725 Receive Task State",
    1002: "Bad Receive Packet Number From 5725",
    1003: "Bad Control Byte From 5725",
    # 1100-Level Faults: 5725 Communication Transmit Subsystem
    1100: "Multiple Timeouts Sending To 5725",
    1101: "5725 Request Reset Loop",
    1102: "Unexpected NSA From 5725",
    1103: "Bad Packet Number In ACK From 5725",
    1104: "Bad Control Byte From 5725 Receive Task",
    1105: "Illegal 5725 Transmit Task State",
    1106: "5725 Indefinite ACKWAIT Holdoff",
    # 1200-Level Faults: 5725 Communication Utility Subsystem
    1200: "Serial Write Failure To 5725",
    1201: "Packet Too Large For 5725",
    # 1300-Level Faults: Output Monitor Subsystem
    1300: "Undefined Signal",
    1301: "Output Tripped To Standby",
    1302: "Output Tripped To Standby",
    1303: "DC Zero Calibration is Needed (Every 30 Days)",
    # 1400-Level Faults: Software Timer Subsystem
    1400: "Cannot Install MTtick()",
    1401: "Bad Timer Selector",
    # 1500-Level Faults: Guard Crossing Receive Subsystem
    1500: "Could not ACK Packet From Inguard",
    1501: "Illegal Inguard Receive Task State",
    1502: "Bad Receive Packet Number From Inguard",
    1503: "Bad Control Byte From Inguard",
    # 1600-Level Faults: Guard Crossing Transmit Subsystem
    1600: "Multiple Timeouts Sending To Inguard",
    1601: "Inguard Request Reset Loop",
    1602: "Unexpected NSA From Inguard",
    1603: "Bad Packet Number In ACK From Inguard",
    1604: "Bad Control Byte From Inguard Rcv Task",
    1605: "Illegal Inguard Transmit Task State",
    1606: "Inguard Indefinite ACKWAIT Holdoff",
    # 1700-Level Faults: Guard Crossing Utility Subsystem
    1700: "Serial Write Failure To Inguard",
    1701: "Packet Too Large For Inguard",
    # 1800-Level Faults: Normal Output Operations Subsystem
    1800: "DORMANT To OPERATE",
    1801: "Bad Transition Type",
    1802: "Bad Boolean Selector",
    1803: "NRMrngStby Encountered Error",
    1804: "DCV Called For Non-DCV Range",
    1805: "ACV Called For Non-ACV Range",
    1806: "Bad ACV Frequency Range",
    1807: "Hi Res Frequency Too High",
    1808: "ACV Amplitude Correction Failure",
    1809: "Cannot Phase Lock To Signal",
    1810: "Bad Phase Quadrant",
    1811: "Current Called for Non-Current Range",
    1812: "Ohms Called for Non-Ohms Range",
    1813: "Cannot Phase Lock, Output Not AC",
    1814: "Cannot Phase Shift, Output Not AC",
    1815: "Bad Wide Band Range",
    1816: "Freq. Too High Even For Wide Band",
    1817: "Illegal Current Output Location",
    1818: "Output Current Out Of Tolerance",
    1819: "Current Compliance Voltage Exceeded",
    1820: "2-Wire Compensation Current Exceeded",
    1821: "NRMbstcur Passed Bad Range",
    1822: "5220 No Longer Connected",
    1823: "5220 Detected Fault And Shut Off",
    1824: "5205 No Longer Connected",
    1825: "5205 Detected Fault And Shut Off",
    1826: "5725 No Longer Connected",
    1827: "14 Bit DAC Scaling Found Zero Output",
    # 1900-Level Faults: Non-Volatile Subsystem
    1900: "Could not Write Byte To EEPROM",
    1901: "Checksum Error Reading EEPROM",
    1902: "EEPROM Is Full",
    1903: "Unknown Nonvolatile Constant Selector",
    1904: "EEPROM Partition Is Full",
    1905: "Could not Write Byte To 5725 EEPROM",
    1906: "Checksum Error Reading 5725 EEPROM",
    # 2000-Level Faults: Analog Operations Manager Subsystem
    2000: "Bad Command Code",
    2001: "Bad Signal",
    2002: "Long Term Command In Progress",
    2003: "Guard Crossing Protocol Failed To Start",
    2004: "Fatal Fault, Output Tripped",
    2005: "Tripped While Calibrating or Diagnosing",
    # 2100-Level Faults: Output Display Driver Subsystem
    2100: "Bad Display Selector",
    2101: "Bad Character",
    2102: "Bad Annunciator Selector",
    # 2200-Level Faults: Remote Interface Subsystem
    2200: "Unknown Command",
    2201: "Invalid Number Of Parameters",
    2202: "Invalid Cal Constant Name",
    2203: "Invalid Keyword",
    2204: "Invalid Range",
    2205: "Invalid Parameter Type",
    2206: "Invalid Parameter Unit",
    2207: "Invalid Parameter Value",
    2208: "IEEE488.2 I/O DEADLOCK",
    2209: "Spare",
    2210: "IEEE488.2 INTERRUPTED Query",
    2211: "Not Allowed From GPIB Interface",
    2212: "Not Allowed From Serial Interface",
    2213: "Remote Only",
    2214: "Invalid Syntax",
    2215: "IEEE488.2 UNTERMINATED Command",
    2216: "Symbol Table Overflow",
    2217: "Command Reserved For Service",
    2218: "Invalid Binary Number",
    2219: "Invalid Binary Block",
    2220: "Invalid Character",
    2221: "Invalid Decimal Number",
    2222: "Invalid Hexadecimal Block",
    2223: "Invalid Hexadecimal Number",
    2224: "Too Many Parameters",
    2225: "Invalid Octal Number",
    2226: "Too Many Characters",
    2227: "Invalid String",
    2228: "Invalid Register Address",
    2229: "Invalid Cal Constant Name",
    2230: "Remote Serial Port Dead",
    2231: "IEEE488.2 Query After Indef Response",
    # 2300-Level Faults: Executive Subsystem
    2300: "Unknown Report Requested",
    2301: "Unknown Report Device Requested",
    2302: "Serial Port Timeout",
    # 2400-Level Faults: Real Time Clock Subsystem
    2400: "Cannot Install Interrupt Handler",
    2401: "Unknown Interrupt Selector",
    2402: "Bad Date & Cannot Be Set",
    # 2500-Level and 2600-Level Faults: Self Calibration Subsystem
    2500: "Invalid Cal Procedure Number",
    2501: "Could not Ext Cal Gain",
    2502: "Could not Ext Cal Ref",
    2503: "Could not Ext Cal Neg",
    2504: "Could not Cal Ratio",
    2505: "Could not Cal Pos 11V or 22V DC Zero",
    2506: "Could not Cal Neg 11V or 22V DC Zero",
    2507: "Could not Cal Pos 11V or 22V DC Gain",
    2508: "Could not Cal 6.5V or 13V Buf Ref",
    2509: "Could not Cal 2.2V Zero",
    2510: "Could not Cal 220 mV Gain S1",
    2511: "Could not Cal 220 mV Gain S2",
    2512: "Could not Cal 220 mV Gain S3",
    2513: "Could not Cal 220 mV Gain S4",
    2514: "Could not Cal 220V DC Offset",
    2515: "Could not Cal 220V DC Rnet",
    2516: "Could not Cal 220V DC Gain",
    2517: "Could not Cal ACV Gain",
    2518: "Flatness Cal: Main Sensor Fail?",
    2519: "Flatness Cal: AC Cal Sensor Fail?",
    2520: "Could not Cal Fine Tune (14 bit) DAC",
    2521: "Could not Cal Fine Tune 15th bit Down",
    2522: "Could not Cal Fine Tune 15th bit Up",
    2523: "Could not Cal 2.2 mV/22 mV step 1",
    2524: "Could not Cal 2.2 mV/22 mV step 2",
    2525: "Could not Cal 220V AC Offset",
    2526: "Could not Cal 220V AC Gain",
    2527: "Could not Cal 220 mV Offset",
    2528: "Could not Cal 1100V AC/DC Offset",
    2529: "Could not Cal 1100V AC/DC Gain",
    2530: "Could not Cal 220 uA DC Zero",
    2531: "Could not Cal 2.2 mA DC Zero",
    2532: "Could not Cal 22 mA DC Zero",
    2533: "Could not Cal 220 mA DC Zero",
    2534: "Could not Cal 2.2A DC Zero",
    2535: "Could not Cal 220 uA DCI (Meas 1)",
    2536: "Could not Cal 220 uA DCI (Meas 2)",
    2537: "Could not Cal 2.2 mA DCI (Meas 1)",
    2538: "Could not Cal 2.2 mA DCI (Meas 2)",
    2539: "Could not Cal 22 mA DCI (Meas 1)",
    2540: "Could not Cal 22 mA DCI (Meas 2)",
    2541: "Could not Cal 220 mA DCI (Meas 1)",
    2542: "Could not Cal 220 mA DCI (Meas 2)",
    2543: "Could not Cal 2.2A DCI (Meas 1)",
    2544: "Could not Cal 2.2A DCI (Meas 2)",
    2545: "Could not Ext Cal 10K Ohm Std (Meas 1)",
    2546: "Could not Ext Cal 10K Ohm Std (Meas 2)",
    2547: "Could not Ext Cal 10K Ohm Std (Meas 3)",
    2548: "Could not Cal 10K Ohm (Meas 1)",
    2549: "Could not Cal 10K Ohm (Meas 2)",
    2550: "Could not Cal 10K Ohm (Meas 3)",
    2551: "Could not Cal 19K Ohm (Meas 1)",
    2552: "Could not Cal 19K Ohm (Meas 2)",
    2553: "Could not Cal 19K Ohm (Meas 3)",
    2554: "Could not Cal 100K Ohm (Meas 1)",
    2555: "Could not Cal 100K Ohm (Meas 2)",
    2556: "Could not Cal 190K Ohm (Meas 1)",
    2557: "Could not Cal 190K Ohm (Meas 2)",
    2558: "Could not Cal 1M Ohm (Meas 1)",
    2559: "Could not Cal 1M Ohm (Meas 2)",
    2560: "Could not Cal 1.9M Ohm (Meas 1)",
    2561: "Could not Cal 1.9M Ohm (Meas 2)",
    2562: "Could not Cal 10M Ohm (Meas 1)",
    2563: "Could not Cal 10M Ohm (Meas 2)",
    2564: "Could not Cal 19M Ohm (Meas 1)",
    2565: "Could not Cal 19M Ohm (Meas 2)",
    2566: "Could not Cal 100M Ohm (Meas 1)",
    2567: "Could not Cal 1K Ohm (Meas 1)",
    2568: "Could not Cal 1K Ohm (Meas 2)",
    2569: "Could not Cal 1.9K Ohm (Meas 1)",
    2570: "Could not Cal 1.9K Ohm (Meas 2)",
    2571: "Could not Cal 100 Ohm (Meas 1)",
    2572: "Could not Cal 100 Ohm (Meas 2)",
    2573: "Could not Cal 190 Ohm (Meas 1)",
    2574: "Could not Cal 190 Ohm (Meas 2)",
    2575: "Could not Cal Ohms Divider (Meas 1)",
    2576: "Could not Cal Ohms Divider (Meas 2)",
    2577: "Could not Cal 10 Ohm (Meas 1)",
    2578: "Could not Cal 10 Ohm (Meas 2)",
    2579: "Could not Cal 19 Ohm (Meas 1)",
    2580: "Could not Cal 19 Ohm (Meas 2)",
    2581: "Could not Ext Cal 1 Ohm (0.26V CM Meas)",
    2582: "Could not Ext Cal 1 Ohm (Ext Meas)",
    2583: "Could not Ext Cal 1 Ohm (0.13V CM Meas)",
    2584: "Could not Ext Cal 1 Ohm (Int Meas)",
    2585: "Could not Ext Cal 1.9 Ohm (0.18V CM Meas)",
    2586: "Could not Ext Cal 1.9 Ohm (0.12V CM Meas)",
    2587: "Could not Ext Cal 1.9 Ohm (Int Meas)",
    2588: "Could not Cal Wideband Gain",
    2589: "Bad Cal Step Selector",
    2590: "A/D Appears To Have Failed",
    2591: "User Range Gain Adjustment Is 0",
    2592: "Stored User Range Gain Adjust Is 0",
    2593: "220 uA AC Flatness Calibration Failed",
    2594: "2.2 mA AC Flatness Calibration Failed",
    2595: "22 mA AC Flatness Calibration Failed",
    2596: "220 mA AC Flatness Calibration Failed",
    2597: "Bad Range Number For IAC Flatness Cal",
    2598: "No Data For DCI Calibration",
    2599: "2.2V AC Flat Early Fail",
    2600: "2.2V AC Flat Bad AC Cal Sensor?",
    2601: "2.2V AC Flat Diverge",
    2602: "22V AC Flat Early Fail",
    2603: "22V AC Flat Bad AC Cal Sensor?",
    2604: "22V AC Flat Diverge",
    2605: "220V AC Flat Early Fail",
    2606: "220V AC Flat Bad AC Cal Sensor?",
    2607: "220V AC Flat Diverge",
    2608: "1100V AC Flat Early Fail",
    2609: "1100V AC Flat Bad AC Cal Sensor?",
    2610: "1100V AC Flat Diverge",
    2611: "220 uA IAC Flat Early Fail",
    2612: "220 uA IAC Flat Bad AC Cal Sensor?",
    2613: "220 uA IAC Flat Diverge",
    2614: "2.2 mA IAC Flat Early Fail",
    2615: "2.2 mA IAC Flat Bad AC Cal Sensor?",
    2616: "2.2 mA IAC Flat Diverge",
    2617: "22 mA IAC Flat Early Fail",
    2618: "22 mA IAC Flat Bad AC Cal Sensor?",
    2619: "22 mA IAC Flat Diverge",
    2620: "220 mA IAC Flat Early Fail",
    2621: "220 mA IAC Flat Bad AC Cal Sensor?",
    2622: "220 mA IAC Flat Diverge",
    2623: "2.2A IAC Flat Early Fail",
    2624: "2.2A IAC Flat Bad AC Cal Sensor?",
    2625: "2.2A IAC Flat Diverge",
    2626: "Illegal Ohms Null Function State",
    2627: "Unknown Source Divider Identifier",
    2628: "No Data For Ohms Calibration",
    2629: "Illegal Ohms Calibration Config",
    2630: "Illegal Number Of Ohms Cal Points",
    2631: "5725 Shunt 0A Meas Fail",
    2632: "5725 Shunt 1.3A Meas Fail",
    2633: "5725 Current Amplifier Zero Meas Fail",
    2634: "5725 Current Amplifier Gain Meas Fail",
    2635: "Wideband Thermal Sensor Cal Diverge",
    2636: "Wideband Sensor Amp Offset Too Big",
    2637: "Wideband Ext Cal Point Bad Const ID",
    2638: "5725 ACV Sense Amp Offset Diverge",
    2639: "5725 ACV Sense Amp Gain Diverge",
    2640: "5725 ACV Flatness Ref Freq Failure",
    2641: "5725 ACV Flatness Cal Freq Failure",
    2642: "External V Reference Value Out Of Range",
    2643: "5725 Diagnostic DAC Cal Didn't Converge",
    2644: "Spare",
    2645: "Spare",
    2646: "DAC Appears To Not Meet Linearity Spec",
    2647: "ACV Buffer Offset Too Large",
    2648: "5725 ACV Flat Test Cap Too Large",
    2649: "5725 ACV Flat Test Cap Too Large",
    2650: "Could not Cal 100M Ohm (Meas 2)",
    # 2700-Level Faults: Self Diagnostics Subsystem (Current)
    2700: "A7: 8255 Control Word",
    2701: "A7: 8255 Port A Fault",
    2702: "A7: 8255 Port B Fault",
    2703: "A7: 8255 Port C Fault",
    2705: "A7: Oven Regulation Fault",
    2706: "A7: Current Compliance Fault",
    2707: "A7: Hardware Initialization Fault",
    2709: "A7: Current Magnitude Fault",
    2710: "A7: Dummy Load Current Fault",
    2711: "Assembly A7 Is Not Responding",
    2712: "A7: Hi-Res Clock Fault",
    2713: "A7: Hi-Res Loop Fault In 100HZ Range",
    2714: "A7: Hi-Res Loop Fault In 1KHZ Range",
    2715: "A7: Hi-Res Loop Fault In 10KHZ Range",
    2716: "A7: Hi-Res Loop Fault In 100KHZ Range",
    2717: "A7: Hi-Res Loop Fault In 1MHZ Range",
    2718: "Fault In Setting Up AC For Diagnostics",
    2719: "A7: 8255 Was Reset",
    # 2800-Level Faults: Self Diagnostics Subsystem (DAC)
    2800: "A11: 8255 Control Word",
    2801: "A11: 8255 Port A Wires",
    2802: "A11: 8255 Port B Data Bus",
    2803: "A11: 82C54 Status Words",
    2804: "A11: DAC Heaters Not Regulated",
    2805: "5700 Not Warmed Up",
    2806: "A11: ADC Amp Output Noise",
    2807: "A11: ADC Amp Output Offset",
    2808: "A11: ADC Amp Gain Error",
    2809: "A11: DAC Monitoring Fault",
    2810: "A11: +11V DC Range Fault",
    2811: "A11: -11V DC Range Fault",
    2812: "A11: +22V DC Range Fault",
    2813: "A11: -22V DC Range Fault",
    2814: "A11: 6.5V Buffered Reference Fault",
    2815: "A11: 6.5V Reference Fault",
    2816: "A11: 13V Buffered Reference Fault",
    2817: "A11: 13V Reference Fault",
    2818: "Assembly A11 Missing",
    2819: "A11: 8255 Was Reset",
    2820: "A11: Fine Adjust Channel Fault",
    2821: "A8/A11: +11/22V DC Zero Estimate Fault",
    2822: "A8/A11: -11/22V DC Zero Estimate Fault",
    2823: "A11: Could not Estimate +11V Or 22V Gain",
    2824: "A11: Could not Estimate 6.5V Or 13V Ref",
    2825: "A11: Could not Estimate 6.5V Or 13V Buf Ref",
    2826: "A11: A/D Overload Fault",
    # 3100-Level Faults: Self Diagnostics Subsystem (High Voltage)
    3100: "A14: 8255 Control Word",
    3101: "A14: 8255 Port A Fault",
    3102: "A14: 8255 Port B Fault",
    3103: "A14: 8255 Port C Fault",
    3104: "A15: HV Oven Regulation Fault",
    3105: "A15: HV I Oven Regulation Fault",
    3106: "A15: DC HV Amp Noise Fault",
    3107: "A15: DC HV Amp Offset Fault",
    3108: "A15: DC HV Amp Gain Fault",
    3109: "A15: HV +DC Preamplifier Fault",
    3110: "A15: HV +DC Series Pass & Current Fault",
    3111: "A15: HV +DC High Voltage Output Fault",
    3112: "A15: HV +DC Ref/Error Amplitude Fault",
    3113: "A15: HV -DC Preamplifier Fault",
    3114: "A15: HV -DC Series Pass & Current Fault",
    3115: "A15: HV -DC High Voltage output Fault",
    3116: "A15: HV -DC Reference/Error Amp Fault",
    3117: "A14/A15: HV +DC Current Error Amp Fault",
    3118: "A14/A15: HV -DC Current Error Amp Fault",
    3119: "A14/A15: HV +DC Current Abs. Value",
    3120: "A14/A15: HV -DC Current Abs. Value",
    3122: "A14/A15: HV AC 1KHZ, Preamp Fault (lo)",
    3123: "A14/A15: HV AC 1KHZ, Preamp Fault (mid)",
    3124: "A14/A15: HV AC 1KHZ, Preamp Fault (hi)",
    3125: "A14/A15: HV AC 100HZ, Preamp Fault (lo)",
    3126: "A14/A15: HV AC 100HZ, Preamp Fault",
    3127: "A14/A15: HV AC 100HZ, Preamp Fault (hi)",
    3128: "A14/A15: HV AC 1KHZ, Output Fault (lo)",
    3129: "A14/A15: HV AC 1KHZ, Output Fault (mid)",
    3130: "A14/A15: HV AC 1KHZ, Output Fault (hi)",
    3131: "A14/A15: HV AC 100HZ, Output Fault (lo)",
    3132: "A14/A15: HV AC 100HZ, Output Fault (mid)",
    3133: "A14/A15: HV AC 100HZ, Output Fault (hi)",
    3135: "Assembly A14 Not Responding",
    3136: "A14: 8255 Was Reset",
    3137: "A14/A15/A16: 2.2A AC Range Mag. Fault",
    3138: "A14/A15/A16: 2.2A AC Range Compliance",
    3139: "A14/A15/A16: 2.2A AC Range Amplitude",
    3140: "A14/A15/A16: 2.2A AC Range Abs. Value",
    3141: "A14/A15/A16: 2.2A DC Range Dummy Load",
    3142: "A14/A15/A16: 2.2A DC Range Compliance",
    3143: "A14/A15/A16: 2.2A DC Range Magnitude",
    # 3200-Level Faults: Self Diagnostics Subsystem (misc)
    3200: "Assemblies Missing",
    3201: "Unknown Diagnostic Test",
    3202: "5725 ACV Sense Amp Fault",
    3203: "5725 ACV Standby 5725 Fault",
    3204: "5725 ACV Operate 5725 Fault",
    3205: "5725 ACV Cal Sensor Test Died",
    3206: "5725 ACV Cal Sensor Fault",
    3207: "5725 ACV VLF (100 Hz) Won't Converge",
    3208: "5725 ACV LF (1 kHz) Won't Converge",
    3209: "5725 ACV MF (10 kHz) Won't Converge",
    3210: "5725 ACV HF (100 kHz) Won't Converge",
    3211: "5725 Current Path To Shunt Open",
    3212: "5725 Shunt Sense Open",
    3213: "5725 Shunt Measurement Out Of Tolerance",
    3214: "5725 Current Amplifier Offset Too Large",
    3215: "5725 Current Drive Path Open",
    3216: "5725 Current Error Amplifier Failure",
    3217: "5725 Amplifer Not Connected",
    # 3300-Level Faults: Self Diagnostics Subsystem (Ohms)
    3300: "A9: 8255 Control Word",
    3301: "A9: 8255 Port A Fault",
    3302: "A9: 8255 Port B Fault",
    3303: "A9: 8255 Port C Fault",
    3304: "A9: 10V Source Fault",
    3305: "A9: 5V Source Fault",
    3306: "A9: 2V Source Fault",
    3308: "A9: Diff Amp Offset Fault",
    3309: "A9: Diff Amp Gain Fault",
    3310: "A9: Diff Amp Noise Fault",
    3311: "A9/A10: Ohms 10:1 Divider Fault",
    3312: "A9/A10: Ohms 1:1 Divider Fault",
    3313: "A9/A10: 10K Ohm Diagnostic Fault",
    3314: "A9/A10: 19K Ohm Cal Diag Fault",
    3315: "A9/A10: 10 Ohm Cal Diag Fault",
    3316: "A9/A10: 19 Ohm Cal Diag Fault",
    3317: "A9/A10: 100K Ohm Ratio Fault",
    3318: "A9/A10: 190K Ohm Ratio Fault",
    3319: "A9/A10: 1M Ohm Ratio Fault",
    3320: "A9/A10: 1.9M Ohm Ratio Fault",
    3321: "A9/A10: 10M Ohm Ratio Fault",
    3322: "A9/A10: 19M Ohm Ratio Fault 1",
    3323: "A9/A10: 19M Ohm Ratio Fault 2",
    3324: "A9/A10: 100M Ohm Ratio Fault",
    3325: "A9/A10: 10K Ohm Check Fault",
    3326: "A9/A10: 19K Ohm Check Fault",
    3327: "A9/A10: 1K Ohm Check Fault",
    3328: "A9/A10: 1.9K Ohm Check Fault",
    3329: "A9/A10: 100 Ohm Check Fault",
    3330: "A9/A10: 190 Ohm Check Fault",
    3331: "A9/A10: 10 Ohm Check Fault",
    3332: "A9/A10: 19 Ohm Check Fault",
    3333: "A9/A10: 1 Ohm Check Fault",
    3334: "A9/A10: 1.9 Ohm Check Fault",
    3335: "A9/A10: Ohms Short Check Fault",
    3336: "A9/A10: 2 Wire Compensation Fault",
    3337: "A9/A10: Ohms Correction Factor Fault",
    3338: "Assembly A9 Not Responding",
    3339: "A9: 8255 Was Reset",
    3340: "A9/A10: 100 Ohm Cal Diag Fault",
    3341: "A9/A10: 190 Ohm Cal Diag Fault",
    # 3400-Level Faults: Self Diagnostics Subsystem (Oscillator)
    3400: "A12: 8255 Control Word",
    3401: "A12: 8255 Port A",
    3402: "A12: 8255 Port B",
    3403: "A12: 8255 Port C",
    3404: "A13: 8255 Control Word",
    3405: "A13: 8255 Port A",
    3406: "A13: 8255 Port B",
    3407: "A13: 8255 Port C",
    3408: "A13: Fixed Ampl. Osc Fault",
    3409: "A13: Phase Lock Loop Fault",
    3410: "A12/A13: 22V Amp Bias Adj Error",
    3411: "A12/A13: 22V Amp Nonfunctional",
    3412: "A12 To A13 Interface Fault",
    3413: "A12/A13: 14 bit DAC Nonfunctional",
    3414: "A12/A13: Nonlinear Control Loop 2Vrng",
    3415: "A12/A13: nonlinear Control Loop 20Vrng",
    3416: "A12/A13: DAC 15th Bit Fault",
    3417: "A12: DC Sensor Buffer Fault",
    3418: "A12: Sensor Loop/Sq. Root Amp Fault",
    3419: "A12: AC Sensor Buff (2V Range)",
    3420: "A12: AC Sensor Buff (20V Range)",
    3421: "A12: AC Cal Sensor (2V Range)",
    3422: "A12: AC Cal Sensor (20V Range)",
    3423: "Assembly A12 Missing",
    3424: "Assembly A13 Missing",
    3425: "Assembly A12 Or A13 Not Responding",
    3426: "A12: 8255 Was Reset",
    3427: "A13: 8255 Was Reset",
    # 3500-Level Faults: Self Diagnostics Subsystem (Power Amp)
    3500: "A16: 8255 Control Word",
    3501: "A16: 8255 Port A",
    3502: "A16: 8255 Port B",
    3503: "A16: 8255 Port C",
    3507: "A16: PA Supplies Are Off",
    3508: "A16/A14: 220V AC Range Output Fault",
    3509: "A16: Amplifier Loop Not Regulated",
    3510: "A16: 220V Amp Fault",
    3511: "A16: Incorrect PA Input",
    3520: "A16: PA Oven Regulation Fault",
    3521: "Assembly A16 Is Not Responding",
    3524: "A16: Power Amp Is Too Hot",
    3525: "220V DC Initialization Fault",
    3526: "220V AC Initialization Fault",
    3527: "A16: Power Amp DC Cal Network Zero Fault",
    3528: "A16: Power Amp DC Cal Network Gain Fault",
    3529: "A16: Pwr Amp 220V Range Attenuator Fault",
    3530: "A16: 8255 Was Reset",
    # 3600-Level Faults: Self Diagnostics Subsystem (Power Supplies)
    3600: "+17S Supply Fault",
    3601: "-17S Supply Fault",
    3602: "+15S Supply Fault",
    3603: "-15S Supply Fault",
    3604: "+42S Supply Fault",
    3605: "-42S Supply Fault",
    3606: "LH COM Ground Fault",
    3607: "-5LH Supply Fault",
    3608: "5RLH Supply Fault",
    3609: "8RLH Supply Fault",
    3610: "+PASupply Fault",
    3611: "-PASupply Fault",
    3612: "+15 OSC Supply Fault",
    3613: "-15 OSC Supply Fault",
    3614: "OSC COM Ground Fault",
    3615: "S COM Ground Fault",
    # 3700-Level Faults: Self Diagnostics Subsystem (Rear I/O)
    3700: "A21: 8255 Control Word",
    3701: "A21: 8255 Port A Fault",
    3702: "A21: 8255 Port B Fault",
    3703: "A21: 8255 Port C Fault",
    3704: "Assembly A21 Not Responding",
    3705: "A21: Rear Panel Data Bus Fault",
    3708: "A21: 8255 Was Reset",
    # 3800-Level Faults: Self Diagnostics Subsystem (Switch Matrix)
    3800: "A8: 8255 Control Word",
    3801: "A8: 8255 Port A Fault",
    3802: "A8: 8255 Port B Fault",
    3803: "A8: 8255 Port C Fault",
    3804: "A8: Zero Amp Lo Noise Fault",
    3805: "A8: Zero Amp Lo Offset",
    3806: "A8: Zero Amp Lo Gain Fault",
    3807: "A8: Zero Amp Hi Noise Fault",
    3808: "A8: Zero Amp Hi Offset",
    3809: "A8: Zero Amp Hi Gain Fault",
    3810: "A8: 2.2V Amp Noise Fault",
    3811: "A8: Zero Amp Offset",
    3812: "A8: 2.2V Gain Fault",
    3813: "A8: 220mV Offset Fault",
    3814: "A8: 220mV Divider Fault",
    3815: "A8: 22mV Divider Fault",
    3818: "A8: Out Lo To Sense Lo Continuity Fault",
    3819: "A8: Relay Fault",
    3824: "A8: Oven Regulation Fault",
    3825: "Assembly A8 Not Responding",
    3826: "A8: 8255 Was Reset",
    3827: "Assembly A8 Too Hot",
    # 3900-Level Faults: Self Diagnostics Subsystem (Wideband)
    3900: "A5: 8255 Control Word",
    3901: "A5: 8255 Port A Fault",
    3902: "A5: 8255 Port B Fault",
    3903: "A5: 8255 Port C Fault",
    3904: "Optional Assemblies A5/A6 Are Missing",
    3905: "A6: Phase Lock Loop Fault",
    3906: "A6: Phase Lock Loop Fault At 30Mhz",
    3907: "A5: RMS Sensor Fault",
    3908: "A5: RMS Sensor Fault At 30Mhz",
    3909: "A5: RMS Sensor Fault At 6.5V DC in",
    3910: "A5/A6: Ampl. Control Fault",
    3911: "A5/A6: Ampl. Control Fault At 30Mhz",
    3912: "A5/A6: Output Offset Fault",
    3913: "A5/A6: Output Offset Fault At 30Mhz",
    3914: "A5: 0DB Output Attenuation Fault",
    3915: "A5: 10DB Output Attenuation Fault",
    3916: "A5: 20DB Output Attenuation Fault",
    3917: "A5: 30DB Output Attenuation Fault",
    3918: "A5: 40DB Output Attenuation Fault",
    3922: "A5/A6: Wideband Initialization Fault",
    3923: "A5: 8255 Was Reset",
    # 4000-Level Faults: Analog Sequencing Subsystem
    4000: "Bad Sequence ID",
    4001: "Over Nested Subsequences",
    4002: "Bad Sequence Command Code",
    4003: "Bad Reply Size From Inguard",
    4004: "Reply From Inguard Too Small",
    4005: "False MSG Semaphore from Inguard",
    4006: "Inguard CPU POP",
    4007: "Inguard CPU Reset",
    4008: "Inguard CPU A/D Timeout",
    4009: "Inguard CPU Timed Out On Main CPU",
    4010: "Inguard CPU Detected A Command Error",
    4011: "Bad Y5000 Get/Set Selector",
    4012: "Sequencer Timed Out Waiting For Inguard",
    4013: "Illegal Analog State Command For 5725",
    4014: "Sequencer Timed Out Waiting for 5725",
    4015: "Could not Queue Command To 5725 BX",
    4016: "Reply From 5725 Was Garbled",
    # 4100-Level Faults: Serial Interface Driver Subsystem
    4100: "Bad Virtual channel",
    4101: "Framing",
    4102: "Input Queue Overflow",
    4103: "Overrun",
    4104: "Parity",
    4105: "Uart Failed Self Test",
    4106: "Serial Port 0",
    4107: "Serial Port 1",
    4108: "Guard Crossing UART",
    4109: "Serial Port 3",
    4110: "Remote Interface UART",
    4111: "Boost Crossing UART",
    # 4200-Level Faults: Instrument State Manager Subsystem
    4200: "Bad Boolean Value Selector",
    4201: "Meaningless Target State Value",
    4202: "Meaningless Actual State Value",
    4203: "Mystery Target/Actual State Difference",
    # 4300-Level Faults: Self Test Subsystem
    4301: "ROM Checksum",
    4302: "RAM",
    4303: "Real Time Clock",
    4304: "Keyboard",
    4305: "Output Display",
    4306: "Control Display",
    4307: "IEEE488",
    4308: "Rear Panel DUART",
    4309: "Guard Crossing DUART",
    4310: "Interrupt Hardware",
    4311: "Inguard CPU ROM Fault",
    4312: "Inguard CPU RAM Fault",
    4313: "Front Panel Refresh Fault",
    # 4400-Level Faults: General Purpose Utility Subsystem
    4400: "Invalid Command",
    # 4500-Level Faults: Analog Value Finder Subsystem
    4500: "VFdcDac Value Out Of DAC Range",
    4501: "Bad Range Selector",
    4502: "Non-Vfinder Range",
    4503: "Bad DAC ICALL",
    4504: "User Range Adjust of 0 (I used 1)",
    4505: "Divide By 0 In (VF)correct",
    # 4600-Level Faults: Watch Dog Monitoring Subsystem
    4600: "Watchdog Timed Out",
    # 4700-Level Faults: Operating Kernel Subsystem
    4700: "Kstart Returned",
    4701: "Task Termination",
    4702: "No Signal Handler",
    4703: "Ksuspend( ) When Not RUNNING",
    4704: "Ksleep( ) When Not RUNNING",
    4705: "Kevwait( ) When Not RUNNING",
}

FAULT_CODES = {**_SERVICE_MANUAL_FAULTS, **FAULT_CODES}

#: Fault logged when a state-changing command arrives while a long-term
#: command is running (guide §7.4). ``STBY`` is such a command, which is why
#: there is no remote emergency stop — see :meth:`Fluke5700A._run_long_term`.
FAULT_LONG_TERM_IN_PROGRESS = 2002

#: "Unknown Command" — how the calibrator rejects a misspelled *command*.
#: An unknown *query* instead produces **no response at all** (gap G31), so
#: the two spelling probes in this module look for different symptoms.
FAULT_UNKNOWN_COMMAND = 2200

#: "Calibration Steps Out Of Order" — the ``CAL_REF`` sequence guard
#: (guide §14.6). This module deliberately does not enforce the sequence;
#: ordering belongs to the calibration procedure layer.
FAULT_CAL_STEPS_OUT_OF_ORDER = 852

#: Faults that mean a command was refused because the rear-panel CALIBRATION
#: switch is in NORMAL (guide §7.6, §12; gap G29 — the switch cannot be read
#: remotely, so these codes are the only evidence). 850/851 are the **5725A's
#: own** switch, which is a separate physical switch.
CALIBRATION_SWITCH_FAULT_CODES = frozenset({838, 839, 845, 850, 851, 855})

#: Faults that mean the analog output dropped itself to standby (guide §11.7).
TRIP_FAULT_CODES = frozenset({210, 222, 224, 227, 235, 1301, 1302, 2004, 2005})


def explain_fault(code: int) -> str:
    """Offline decode of a fault code via :data:`FAULT_CODES`."""
    return FAULT_CODES.get(code, f"unknown fault code {code}")


# ──────────────────────────────────────────────────────────────────────────
# Output envelope (guide §11.1–§11.3, §18.1)
# ──────────────────────────────────────────────────────────────────────────

#: Relative slack on every envelope comparison. Amplitudes arrive here after a
#: decimal → binary round trip, so a plan asking for exactly 10 % of range must
#: not be rejected by one ulp.
_ENVELOPE_REL_TOL = 1e-9


def _le(value: float, limit: float) -> bool:
    """``value <= limit`` with :data:`_ENVELOPE_REL_TOL` slack."""
    return value <= limit + abs(limit) * _ENVELOPE_REL_TOL


def _ge(value: float, limit: float) -> bool:
    """``value >= limit`` with :data:`_ENVELOPE_REL_TOL` slack."""
    return value >= limit - abs(limit) * _ENVELOPE_REL_TOL


def _same(value: float, target: float) -> bool:
    return math.isclose(value, target, rel_tol=_ENVELOPE_REL_TOL,
                        abs_tol=1e-12)


@dataclass(frozen=True)
class OutputRange:
    """One selectable output range (guide §11.2, tabulated in §18.1).

    *minimum* is the smallest magnitude the range can source — 0 for dc, 10 %
    of full scale for most ac ranges, and the two documented exceptions
    (100 V on the dc 1100 V range, 9 µA on the ac 220 µA range).

    *freq_min* / *freq_max* are 0 for dc-only ranges. *amplitude_caps* holds
    ``(f_lo, f_hi, max_amplitude)`` triples for frequency bands where the
    range cannot reach full scale — the ac 1100 V range is capped at 250 V
    from 15 to 50 Hz.
    """

    range_id: str
    full_scale: float
    minimum: float = 0.0
    freq_min: float = 0.0
    freq_max: float = 0.0
    amplitude_caps: "tuple[tuple[float, float, float], ...]" = ()

    @property
    def is_ac(self) -> bool:
        return self.freq_max > 0.0

    def cap_for(self, frequency_hz: float) -> float:
        """Maximum amplitude this range can source at *frequency_hz*."""
        for f_lo, f_hi, cap in self.amplitude_caps:
            if f_lo <= frequency_hz <= f_hi:
                return cap
        return self.full_scale


#: Function-wide ac-voltage frequency limits (guide §11.1, §11.2). The upper
#: figure is 1.1999 MHz, **not** 1.2 MHz; the §11.2 warning box and §13 say to
#: treat it as the hard ceiling and let faults 818/819 decide the rest rather
#: than hard-coding a cliff at 1.1 MHz.
AC_VOLTS_FREQ_MIN_HZ = 10.0
AC_VOLTS_FREQ_MAX_HZ = 1.1999e6

#: Function-wide ac-current frequency limits (guide §11.2). Per-range limits
#: are tighter — the 2.2 A range starts at 20 Hz.
AC_CURRENT_FREQ_MIN_HZ = 10.0
AC_CURRENT_FREQ_MAX_HZ = 10.0e3

#: The ac-voltage envelope is additionally bounded by 2.2E7 V·Hz
#: ([OM Figure A, p.1-28]; guide §11.2). Programming outside it produces fault
#: 820. The figure is a *graph* — gap G15 records that the manuals never
#: tabulate the breakpoints, so this check is necessarily approximate and the
#: instrument's own fault 820 remains the authority.
VOLT_HERTZ_MAX = 2.2e7

DC_VOLTS_RANGES = (
    OutputRange("DC220MV", 0.22),
    OutputRange("DC2_2V", 2.2),
    OutputRange("DC11V", 11.0),
    OutputRange("DC22V", 22.0),
    OutputRange("DC220V", 220.0),
    # The only dc-voltage range with a non-zero minimum (guide §11.2).
    OutputRange("DC1100V", 1100.0, minimum=100.0),
)

AC_VOLTS_RANGES = (
    OutputRange("AC2_2MV", 2.2e-3, 2.2e-4,
                AC_VOLTS_FREQ_MIN_HZ, AC_VOLTS_FREQ_MAX_HZ),
    OutputRange("AC22MV", 22e-3, 2.2e-3,
                AC_VOLTS_FREQ_MIN_HZ, AC_VOLTS_FREQ_MAX_HZ),
    OutputRange("AC220MV", 0.22, 0.022,
                AC_VOLTS_FREQ_MIN_HZ, AC_VOLTS_FREQ_MAX_HZ),
    OutputRange("AC2_2V", 2.2, 0.22,
                AC_VOLTS_FREQ_MIN_HZ, AC_VOLTS_FREQ_MAX_HZ),
    OutputRange("AC22V", 22.0, 2.2,
                AC_VOLTS_FREQ_MIN_HZ, AC_VOLTS_FREQ_MAX_HZ),
    OutputRange("AC220V", 220.0, 22.0,
                AC_VOLTS_FREQ_MIN_HZ, AC_VOLTS_FREQ_MAX_HZ),
    # 15 Hz - 1 kHz only, and capped at 250 V from 15 to 50 Hz
    # ([OM p.1-19] note 1; guide §11.2, §18.1).
    OutputRange("AC1100V", 1100.0, 110.0, 15.0, 1.0e3,
                amplitude_caps=((15.0, 50.0, 250.0),)),
)

DC_CURRENT_RANGES = (
    OutputRange("DC220UA", 220e-6),
    OutputRange("DC2_2MA", 2.2e-3),
    OutputRange("DC22MA", 22e-3),
    # [OM Table 5-4] prints this as "DC 220A"; guide §10 correction 1.
    OutputRange("DC220MA", 0.22),
    OutputRange("DC2_2A", 2.2),
)

AC_CURRENT_RANGES = (
    # 9 uA, not 10 % of range -- the documented exception (guide §11.2).
    OutputRange("AC220UA", 220e-6, 9e-6,
                AC_CURRENT_FREQ_MIN_HZ, AC_CURRENT_FREQ_MAX_HZ),
    OutputRange("AC2_2MA", 2.2e-3, 2.2e-4,
                AC_CURRENT_FREQ_MIN_HZ, AC_CURRENT_FREQ_MAX_HZ),
    OutputRange("AC22MA", 22e-3, 2.2e-3,
                AC_CURRENT_FREQ_MIN_HZ, AC_CURRENT_FREQ_MAX_HZ),
    OutputRange("AC220MA", 0.22, 0.022,
                AC_CURRENT_FREQ_MIN_HZ, AC_CURRENT_FREQ_MAX_HZ),
    # Specified only from 20 Hz (guide §11.2, §18.1).
    OutputRange("AC2_2A", 2.2, 0.22, 20.0, AC_CURRENT_FREQ_MAX_HZ),
)

#: The 17 standard resistances plus a short, with their §10 range identifiers
#: (guide §11.2; [OM §4-29, p.4-30]). Resistance is a **discrete set, not a
#: continuum** — anything else faults. ``OHM1_9K`` is guide §10 correction 2
#: ([OM Table 5-4] prints ``OHM19K`` twice and omits 1.9 kΩ).
RESISTANCE_VALUES = (
    (0.0, "OHM0"), (1.0, "OHM1"), (1.9, "OHM1_9"), (10.0, "OHM10"),
    (19.0, "OHM19"), (100.0, "OHM100"), (190.0, "OHM190"), (1.0e3, "OHM1K"),
    (1.9e3, "OHM1_9K"), (10.0e3, "OHM10K"), (19.0e3, "OHM19K"),
    (100.0e3, "OHM100K"), (190.0e3, "OHM190K"), (1.0e6, "OHM1M"),
    (1.9e6, "OHM1_9M"), (10.0e6, "OHM10M"), (19.0e6, "OHM19M"),
    (100.0e6, "OHM100M"),
)

#: Two-wire compensation is available only for 19 kΩ and lower
#: (guide §11.2; ``RCOMP``, [OM p.5-36]). Above it, fault 836.
RCOMP_MAX_OHMS = 19.0e3

#: Which binding posts carry the current output — the ``CUR_POST`` parameter
#: list, [OM 1996 Table 5-2, p.176] (text lines 8153-8166): ``NORMAL``
#: selects the OUTPUT HI binding post, ``AUX`` the AUX CURRENT OUTPUT post,
#: ``IB5725`` the 5725A's. **``NORMAL`` is the power-on default and the
#: setting survives power-off** ([OM p.176]: "the calibrator retains the
#: current post setting during power-off periods"; [OM §4-27, p.114]:
#: "NORMAL, which is the OUTPUT HI binding post, also the default setting";
#: [OM §4-26, p.105]: "If you do not select current output location, OUTPUT
#: HI is active"), and ``*RST`` does not touch it — so which post a current
#: point comes out of is an *unread nonvolatile setting* unless something
#: reads or writes it. Mirrored in ``cal/adapters.py`` (import invariant);
#: ``tests/cal/test_adapters.py`` pins the two copies equal.
CUR_POSTS = ("NORMAL", "AUX", "IB5725")

#: Wideband ac (Option 5700A-03), guide §11.2 / §18.1. Kept as data even
#: though no guided application drives the wideband output yet — the service
#: console will. Minimum output is a single figure for the whole function
#: (300 µV = −57 dBm), not 10 % of each range.
#: Wideband (Option 5700A-03) frequency envelope: 10 Hz to 30 MHz
#: ([MS5] change #1; guide §11.2). The output is ac only.
WIDEBAND_FREQ_MIN_HZ = 10.0
WIDEBAND_FREQ_MAX_HZ = 30.0e6

WIDEBAND_RANGES = (
    OutputRange("WB1_1MV", 1.1e-3, 300e-6, 10.0, 30.0e6),
    OutputRange("WB3_3MV", 3.3e-3, 300e-6, 10.0, 30.0e6),
    OutputRange("WB11MV", 11e-3, 300e-6, 10.0, 30.0e6),
    OutputRange("WB33MV", 33e-3, 300e-6, 10.0, 30.0e6),
    OutputRange("WB110MV", 0.11, 300e-6, 10.0, 30.0e6),
    OutputRange("WB330MV", 0.33, 300e-6, 10.0, 30.0e6),
    OutputRange("WB1_1V", 1.1, 300e-6, 10.0, 30.0e6),
    OutputRange("WB3_5V", 3.5, 300e-6, 10.0, 30.0e6),
)

#: 5725A auxiliary-amplifier ac-voltage envelope (guide §11.3, [OM Table 1-1]):
#: ``(amp_min, amp_max, freq_min, freq_max)``. Two sub-envelopes — the upper
#: frequency band is limited to 750 V.
FIVE725A_AC_VOLTS_ENVELOPE = (
    (20.0, 1100.0, 40.0, 30.0e3),
    (220.0, 750.0, 30.0e3, 100.0e3),
)

#: 5725A dc current: 0 to ±11 A (guide §11.3), minimum 0 like the base ranges.
FIVE725A_DC_CURRENT_MAX = 11.0

#: 5725A ac current: 1 to 11 A rms, 40 Hz to 10 kHz (guide §11.3, §18.1).
FIVE725A_AC_CURRENT_MIN = 1.0
FIVE725A_AC_CURRENT_MAX = 11.0
FIVE725A_AC_CURRENT_FREQ_MIN_HZ = 40.0
FIVE725A_AC_CURRENT_FREQ_MAX_HZ = 10.0e3

#: Relative slack allowed when snapping a characterized ``OUT?`` resistance
#: readback back onto its nominal catalogue value. The calibrator sources a
#: *characterized* resistance close to the requested nominal (guide §11.4
#: item 2), so a readback of 19000.42 Ω is the 19 kΩ range.
RESISTANCE_SNAP_REL_TOL = 0.01


def _normalize_options(options: Sequence[str]) -> "tuple[str, ...]":
    return tuple(str(token).strip().upper() for token in (options or ()))


def _has_5725a(options: Sequence[str]) -> bool:
    """True when the caller declared a 5725A amplifier.

    Matches on the substring ``5725`` so that a raw ``*OPT?`` string
    (``"5725A Attached"``) works as-is.
    """
    return any("5725" in token for token in _normalize_options(options))


class _UnknownRange:
    """Type of :data:`UNKNOWN_RANGE`."""

    def __repr__(self) -> str:
        return "<unknown range>"


#: Returned by :func:`_declared_range` when a range identifier **was** given
#: but is not one this function has. Deliberately distinct from ``None``
#: ("no range pinned"): collapsing the two would make a typo read as "no
#: constraint", silently dropping the per-range checks the caller asked for
#: and re-enabling the 5725A suppression in :func:`_validate_ac_volts`.
UNKNOWN_RANGE = _UnknownRange()

#: §10 identifiers for the resistance function (:data:`RESISTANCE_VALUES`).
RESISTANCE_RANGE_IDS = frozenset(rid for _, rid in RESISTANCE_VALUES)


def _declared_range(range_id: Optional[str],
                    table: "tuple[OutputRange, ...]"):
    """Look *range_id* up in *table*.

    Naming a §10 range identifier is how a caller says "the range is pinned
    here" — that is the only way the dc 1100 V range's 100 V minimum, or any
    other per-range minimum, can be violated: with autoranging the calibrator
    picks a range that can reach the requested value.

    Returns ``None`` when no range was named, the :class:`OutputRange` when it
    was recognized, and :data:`UNKNOWN_RANGE` when it was named but is not in
    *table* — a caller that names a range is asserting something, and failing
    to recognize it must never read as "no constraint".
    """
    if not range_id:
        return None
    wanted = str(range_id).strip().upper()
    for candidate in table:
        if candidate.range_id == wanted:
            return candidate
    return UNKNOWN_RANGE


def _unknown_range_problem(range_id: Optional[str]) -> "list[str]":
    return [f"unknown range identifier {range_id!r} for this function; a "
            f"pinned range must be one the function actually has (guide §10). "
            f"Refusing to fall back to autoranging: that would silently drop "
            f"the per-range limits you asked to be checked"]


def snap_resistance(value: float) -> Optional[float]:
    """Nearest catalogue resistance to a characterized ``OUT?`` readback.

    ``OUT?`` in the ohms function returns the value the calibrator is really
    sourcing, not the nominal that was requested (guide §11.4 item 2), so a
    readback must never be compared against a nominal limit directly.
    Returns ``None`` when *value* is not within
    :data:`RESISTANCE_SNAP_REL_TOL` of any catalogue entry.

    The short (nominal 0 Ω) has no meaningful *relative* error, so it is
    matched on **absolute** error against the same tolerance figure: any
    readback with ``|value| <= RESISTANCE_SNAP_REL_TOL`` ohms — 0.01 Ω at the
    default — snaps to the short. That is well inside the 1 Ω range's own
    spacing, so it cannot swallow a neighbouring catalogue value.
    """
    best: Optional[float] = None
    best_error: Optional[float] = None
    for nominal, _ in RESISTANCE_VALUES:
        # The short has no meaningful relative error; compare it absolutely.
        error = (abs(value) if nominal == 0.0
                 else abs(value - nominal) / nominal)
        if best_error is None or error < best_error:
            best, best_error = nominal, error
    if best_error is not None and best_error <= RESISTANCE_SNAP_REL_TOL:
        return best
    return None


def _select_range(table: "tuple[OutputRange, ...]",
                  magnitude: float) -> Optional[OutputRange]:
    """The range the calibrator autoranges to for *magnitude*, or ``None``."""
    for candidate in table:
        if _le(magnitude, candidate.full_scale):
            return candidate
    return None


def _fits_5725a_ac_volts(magnitude: float, frequency_hz: float) -> bool:
    return any(_ge(magnitude, lo) and _le(magnitude, hi)
               and _ge(frequency_hz, f_lo) and _le(frequency_hz, f_hi)
               for lo, hi, f_lo, f_hi in FIVE725A_AC_VOLTS_ENVELOPE)


def expected_range_id(amplitude: float, unit: str, frequency_hz: float = 0.0,
                      *, options: Sequence[str] = (),
                      wideband: bool = False) -> Optional[str]:
    """Predict the §10 range identifier the calibrator will select.

    A *prediction* from the tables above, used for settling-time estimates and
    for human-readable envelope messages. ``RANGE?``
    (:meth:`Fluke5700A.range_id`) is always the authority — guide §10 warns
    that two identifiers are misprinted in [OM Table 5-4] and recommends
    enumerating them from the firmware at run time. Returns ``None`` when no
    range can source the request.

    *options* declares installed capability only (a 5725A amplifier); it is
    never a place to name a range.
    """
    unit_u = str(unit).strip().upper()
    magnitude = abs(amplitude)

    if wideband:
        if unit_u != "V" or frequency_hz <= 0:
            return None
        selected = _select_range(WIDEBAND_RANGES, magnitude)
        return selected.range_id if selected is not None else None

    if unit_u == "OHM":
        for value, range_id in RESISTANCE_VALUES:
            if _same(magnitude, value):
                return range_id
        return None

    if unit_u == "V":
        table = AC_VOLTS_RANGES if frequency_hz > 0 else DC_VOLTS_RANGES
    elif unit_u == "A":
        table = AC_CURRENT_RANGES if frequency_hz > 0 else DC_CURRENT_RANGES
    else:
        return None

    selected = _select_range(table, magnitude)
    if selected is not None and (
            not selected.is_ac
            or (_ge(frequency_hz, selected.freq_min)
                and _le(frequency_hz, selected.freq_max)
                and _le(magnitude, selected.cap_for(frequency_hz)))):
        return selected.range_id

    # Base instrument cannot reach it; an amplifier might.
    if _has_5725a(options):
        if unit_u == "A":
            return "AC5725A" if frequency_hz > 0 else "DC5725A"
        if frequency_hz > 0 and _fits_5725a_ac_volts(magnitude, frequency_hz):
            return "AC5725V"
    return selected.range_id if selected is not None else None


def _fmt(value: float) -> str:
    """Compact rendering of an envelope limit for a message."""
    return f"{value:g}"


def _boost_unsupported(request: str) -> str:
    """The refusal for a point only a 5725A amplifier can source.

    The envelope tables carry the amplifier's corner so it can be *named*,
    but engaging it takes ``BOOST``/``BTYPE`` (guide §5) and this driver
    implements neither — declaring the option must therefore never turn an
    amplifier-only point into a promise. Without this refusal the request
    passes offline validation and then faults 801/841 at the instrument.
    """
    return (f"{request} is reachable only through the attached 5725A "
            f"amplifier, and this driver does not implement BOOST/BTYPE, so "
            f"an amplifier output cannot be engaged; sending it would fault "
            f"801/841 at the instrument")


def _validate_dc_volts(amplitude: float,
                       range_id: Optional[str]) -> "list[str]":
    problems: "list[str]" = []
    magnitude = abs(amplitude)
    declared = _declared_range(range_id, DC_VOLTS_RANGES)
    if declared is UNKNOWN_RANGE:
        return _unknown_range_problem(range_id)
    selected = declared or _select_range(DC_VOLTS_RANGES, magnitude)
    if selected is None:
        problems.append(
            f"{_fmt(magnitude)} V exceeds the 1100 V dc-voltage maximum "
            f"(fault 816)")
        return problems
    if not _le(magnitude, selected.full_scale):
        problems.append(
            f"{_fmt(magnitude)} V exceeds the "
            f"{_fmt(selected.full_scale)} V full scale of the declared "
            f"{selected.range_id} range (fault 803/816)")
    if not _ge(magnitude, selected.minimum):
        problems.append(
            f"{_fmt(magnitude)} V is below the {_fmt(selected.minimum)} V "
            f"minimum output of the {selected.range_id} range "
            f"(fault 804/817)")
    return problems


def _validate_ac_volts(amplitude: float, frequency_hz: float,
                       options: Sequence[str],
                       range_id: Optional[str]) -> "list[str]":
    # Complaints about the *request itself* rather than about which envelope
    # it has to fit inside. These are never withdrawn: a negative rms
    # amplitude is malformed no matter which amplifier is attached, and
    # withdrawing it would put "OUT -5.0000000000E+02 V" on the wire.
    malformed: "list[str]" = []
    magnitude = abs(amplitude)
    if amplitude <= 0:
        malformed.append(
            f"ac amplitude must be a positive rms value, got "
            f"{_fmt(amplitude)} V")

    # Complaints about the base-instrument envelope. Only these are
    # withdrawable, and only when an amplifier covers the point.
    envelope: "list[str]" = []
    if not _ge(frequency_hz, AC_VOLTS_FREQ_MIN_HZ):
        envelope.append(
            f"{_fmt(frequency_hz)} Hz is below the "
            f"{_fmt(AC_VOLTS_FREQ_MIN_HZ)} Hz ac-voltage minimum (fault 819)")
    if not _le(frequency_hz, AC_VOLTS_FREQ_MAX_HZ):
        envelope.append(
            f"{_fmt(frequency_hz)} Hz is above the "
            f"{_fmt(AC_VOLTS_FREQ_MAX_HZ)} Hz ac-voltage maximum (fault 818)")

    declared = _declared_range(range_id, AC_VOLTS_RANGES)
    if declared is UNKNOWN_RANGE:
        return _unknown_range_problem(range_id)
    selected = declared or _select_range(AC_VOLTS_RANGES, magnitude)
    if selected is None:
        envelope.append(
            f"{_fmt(magnitude)} V exceeds the 1100 V ac-voltage maximum "
            f"(fault 816)")
    else:
        cap = selected.cap_for(frequency_hz)
        if not _le(magnitude, cap):
            if cap < selected.full_scale:
                envelope.append(
                    f"{_fmt(magnitude)} V exceeds the {_fmt(cap)} V limit of "
                    f"the {selected.range_id} range between 15 Hz and 50 Hz "
                    f"(fault 820)")
            else:
                envelope.append(
                    f"{_fmt(magnitude)} V exceeds the {_fmt(cap)} V full "
                    f"scale of the {selected.range_id} range "
                    f"(fault 803/816)")
        if not _ge(magnitude, selected.minimum):
            envelope.append(
                f"{_fmt(magnitude)} V is below the {_fmt(selected.minimum)} V "
                f"minimum output (10 % of range) of the {selected.range_id} "
                f"range (fault 804/817)")
        if not (_ge(frequency_hz, selected.freq_min)
                and _le(frequency_hz, selected.freq_max)):
            envelope.append(
                f"{_fmt(frequency_hz)} Hz is outside the "
                f"{_fmt(selected.freq_min)}-{_fmt(selected.freq_max)} Hz "
                f"limits of the {selected.range_id} range (fault 818/819)")

    if magnitude * frequency_hz > VOLT_HERTZ_MAX * (1 + _ENVELOPE_REL_TOL):
        envelope.append(
            f"{_fmt(magnitude)} V x {_fmt(frequency_hz)} Hz = "
            f"{magnitude * frequency_hz:.3E} V*Hz exceeds the "
            f"{VOLT_HERTZ_MAX:.3E} V*Hz envelope (fault 820)")

    # The 5725A extends the high-voltage / high-frequency corner (guide §11.2,
    # §11.3), which the manuals never redraw. A request outside the base
    # envelope but inside the amplifier's is *recognized* — and refused with
    # the real reason (no BOOST support) instead of the misleading
    # base-envelope complaints. A pinned range keeps its own complaints:
    # naming a range is the caller asserting a base-instrument range, not
    # the amplifier.
    if (envelope and not malformed and declared is None
            and _has_5725a(options)
            and _fits_5725a_ac_volts(magnitude, frequency_hz)):
        envelope = [_boost_unsupported(
            f"{_fmt(magnitude)} V at {_fmt(frequency_hz)} Hz")]
    return malformed + envelope


def _validate_dc_current(amplitude: float, options: Sequence[str],
                         range_id: Optional[str]) -> "list[str]":
    problems: "list[str]" = []
    magnitude = abs(amplitude)
    declared = _declared_range(range_id, DC_CURRENT_RANGES)
    if declared is UNKNOWN_RANGE:
        return _unknown_range_problem(range_id)
    selected = declared or _select_range(DC_CURRENT_RANGES, magnitude)
    if selected is None:
        if not _has_5725a(options):
            problems.append(
                f"{_fmt(magnitude)} A exceeds the 2.2 A dc-current maximum of "
                f"the 5700A; a 5725A amplifier (to 11 A) is not declared in "
                f"options (fault 801/841)")
        elif not _le(magnitude, FIVE725A_DC_CURRENT_MAX):
            problems.append(
                f"{_fmt(magnitude)} A exceeds the "
                f"{_fmt(FIVE725A_DC_CURRENT_MAX)} A 5725A dc-current maximum "
                f"(fault 816)")
        else:
            problems.append(_boost_unsupported(f"{_fmt(magnitude)} A dc"))
        return problems
    if not _le(magnitude, selected.full_scale):
        problems.append(
            f"{_fmt(magnitude)} A exceeds the "
            f"{_fmt(selected.full_scale)} A full scale of the declared "
            f"{selected.range_id} range (fault 803/816)")
    if not _ge(magnitude, selected.minimum):
        problems.append(
            f"{_fmt(magnitude)} A is below the {_fmt(selected.minimum)} A "
            f"minimum output of the {selected.range_id} range "
            f"(fault 804/817)")
    return problems


def _validate_ac_current(amplitude: float, frequency_hz: float,
                         options: Sequence[str],
                         range_id: Optional[str]) -> "list[str]":
    problems: "list[str]" = []
    magnitude = abs(amplitude)
    if amplitude <= 0:
        problems.append(
            f"ac amplitude must be a positive rms value, got "
            f"{_fmt(amplitude)} A")
    if not _ge(frequency_hz, AC_CURRENT_FREQ_MIN_HZ):
        problems.append(
            f"{_fmt(frequency_hz)} Hz is below the "
            f"{_fmt(AC_CURRENT_FREQ_MIN_HZ)} Hz ac-current minimum "
            f"(fault 819)")
    if not _le(frequency_hz, AC_CURRENT_FREQ_MAX_HZ):
        problems.append(
            f"{_fmt(frequency_hz)} Hz is above the "
            f"{_fmt(AC_CURRENT_FREQ_MAX_HZ)} Hz ac-current maximum "
            f"(fault 818)")

    declared = _declared_range(range_id, AC_CURRENT_RANGES)
    if declared is UNKNOWN_RANGE:
        return _unknown_range_problem(range_id)
    selected = declared or _select_range(AC_CURRENT_RANGES, magnitude)
    if selected is None:
        if not _has_5725a(options):
            problems.append(
                f"{_fmt(magnitude)} A exceeds the 2.2 A ac-current maximum of "
                f"the 5700A; a 5725A amplifier (1 A to 11 A) is not declared "
                f"in options (fault 801/841)")
            return problems
        fits_amplifier = True
        if not (_ge(magnitude, FIVE725A_AC_CURRENT_MIN)
                and _le(magnitude, FIVE725A_AC_CURRENT_MAX)):
            fits_amplifier = False
            problems.append(
                f"{_fmt(magnitude)} A is outside the "
                f"{_fmt(FIVE725A_AC_CURRENT_MIN)}-"
                f"{_fmt(FIVE725A_AC_CURRENT_MAX)} A 5725A ac-current range "
                f"(fault 816/817)")
        if not (_ge(frequency_hz, FIVE725A_AC_CURRENT_FREQ_MIN_HZ)
                and _le(frequency_hz, FIVE725A_AC_CURRENT_FREQ_MAX_HZ)):
            fits_amplifier = False
            problems.append(
                f"{_fmt(frequency_hz)} Hz is outside the "
                f"{_fmt(FIVE725A_AC_CURRENT_FREQ_MIN_HZ)}-"
                f"{_fmt(FIVE725A_AC_CURRENT_FREQ_MAX_HZ)} Hz 5725A "
                f"ac-current band (fault 818/819)")
        if fits_amplifier:
            problems.append(_boost_unsupported(
                f"{_fmt(magnitude)} A at {_fmt(frequency_hz)} Hz"))
        return problems

    if not _le(magnitude, selected.full_scale):
        problems.append(
            f"{_fmt(magnitude)} A exceeds the "
            f"{_fmt(selected.full_scale)} A full scale of the declared "
            f"{selected.range_id} range (fault 803/816)")
    if not _ge(magnitude, selected.minimum):
        problems.append(
            f"{_fmt(magnitude)} A is below the {_fmt(selected.minimum)} A "
            f"minimum output of the {selected.range_id} range "
            f"(fault 804/817)")
    if not (_ge(frequency_hz, selected.freq_min)
            and _le(frequency_hz, selected.freq_max)):
        problems.append(
            f"{_fmt(frequency_hz)} Hz is outside the "
            f"{_fmt(selected.freq_min)}-{_fmt(selected.freq_max)} Hz limits "
            f"of the {selected.range_id} range (fault 818/819)")
    return problems


def _validate_ohms(amplitude: float, frequency_hz: float,
                   range_id: Optional[str]) -> "list[str]":
    problems: "list[str]" = []
    if range_id:
        wanted = str(range_id).strip().upper()
        if wanted not in RESISTANCE_RANGE_IDS:
            return _unknown_range_problem(range_id)
        if wanted != expected_range_id(amplitude, "OHM"):
            # In the resistance function the range *is* the value, so a pin
            # that names a different one is a contradiction, not a constraint.
            problems.append(
                f"pinned range {wanted} does not match the requested "
                f"{_fmt(amplitude)} OHM; in the resistance function the range "
                f"is the value (guide §10)")
    if frequency_hz != 0:
        problems.append(
            f"a frequency ({_fmt(frequency_hz)} Hz) cannot be set in the ohms "
            f"function (fault 812)")
    if amplitude < 0:
        problems.append(
            f"resistance must not be negative, got {_fmt(amplitude)} OHM")
    elif not any(_same(amplitude, value) for value, _ in RESISTANCE_VALUES):
        problems.append(
            f"{_fmt(amplitude)} OHM is not one of the "
            f"{len(RESISTANCE_VALUES)} catalogue resistances "
            f"(0, 1, 1.9, 10, 19, ... 1E7, 1.9E7, 1E8); resistance is a "
            f"discrete set, not a continuum (fault 820)")
    return problems


def _validate_wideband(amplitude: float, unit_u: str, frequency_hz: float,
                       range_id: Optional[str]) -> "list[str]":
    """The Option 5700A-03 wideband envelope (guide §11, [MS5] change #1).

    A 50 Ω system: 300 µV to 3.5 V rms into 50 Ω, 10 Hz to 30 MHz, ac only.
    Programmed in volts — dBm is an *observable* unit this driver reads back
    (:meth:`Fluke5700A.dbmout_actual`), never one it sources in, because the
    dBm reference changes with the function (§11.4) and a mistake there is a
    factor-of-12 amplitude error.
    """
    if unit_u != "V":
        return [f"the wideband output sources ac volts (into 50 OHM); got "
                f"{unit_u}. Turn wideband off (WBAND OFF) first, or program "
                f"the value in volts"]
    problems: "list[str]" = []
    magnitude = abs(amplitude)
    if amplitude <= 0:
        problems.append(
            f"ac amplitude must be a positive rms value, got "
            f"{_fmt(amplitude)} V")
    declared = _declared_range(range_id, WIDEBAND_RANGES)
    if declared is UNKNOWN_RANGE:
        return _unknown_range_problem(range_id)
    selected = declared or _select_range(WIDEBAND_RANGES, magnitude)
    if selected is None:
        problems.append(
            f"{_fmt(magnitude)} V exceeds the 3.5 V wideband maximum "
            f"(fault 1815)")
    else:
        if not _le(magnitude, selected.full_scale):
            problems.append(
                f"{_fmt(magnitude)} V exceeds the "
                f"{_fmt(selected.full_scale)} V full scale of the declared "
                f"{selected.range_id} range (fault 1815)")
        if not _ge(magnitude, selected.minimum):
            problems.append(
                f"{_fmt(magnitude)} V is below the "
                f"{_fmt(selected.minimum)} V wideband minimum output "
                f"(-57 dBm)")
    if not _ge(frequency_hz, WIDEBAND_FREQ_MIN_HZ):
        problems.append(
            f"{_fmt(frequency_hz)} Hz is below the "
            f"{_fmt(WIDEBAND_FREQ_MIN_HZ)} Hz wideband minimum -- the "
            f"wideband output is ac only (fault 819)")
    if not _le(frequency_hz, WIDEBAND_FREQ_MAX_HZ):
        problems.append(
            f"{_fmt(frequency_hz)} Hz is above the "
            f"{_fmt(WIDEBAND_FREQ_MAX_HZ)} Hz wideband maximum (fault 1816)")
    return problems


def validate_output(amplitude: float, unit: str, frequency_hz: float = 0.0,
                    *, options: Sequence[str] = (),
                    range_id: Optional[str] = None,
                    wideband: bool = False) -> "list[str]":
    """Check a requested output against the envelope. **No I/O.**

    Returns a list of human-readable violations; an empty list means the
    request is inside the envelope. **All** violations are returned, not just
    the first, because a plan-validation caller wants the whole list in one
    pass.

    This is a module-level pure function, not a method, so that a whole test
    plan can be validated with no instrument attached
    (:mod:`caldmm.cal.plan` does exactly that).

    The function is chosen from *unit* and *frequency_hz*: ``V``/``A`` with a
    zero frequency are dc, with a non-zero frequency ac, and ``OHM`` is
    resistance (where any frequency at all is fault 812).

    *options* declares **installed capability only** — the ``*OPT?`` strings,
    e.g. ``["5725A Attached"]``. Without a 5725A declared, 5725A-only
    territory (11 A, ac volts outside the base envelope) is not permitted.

    *range_id* pins a §10 range identifier (``"DC1100V"``, ``"AC22V"``, ...),
    meaning "validate against this range" rather than against the range
    autoranging would pick. It is how a locked range is expressed and the only
    way a per-range minimum such as the dc 1100 V range's 100 V floor can be
    violated. The two namespaces are deliberately separate: a range must never
    be inferred from a capability string, or a unit that merely *has* an
    amplifier starts being validated as though it were using it.

    Encoded from guide §11.1-§11.3 and §18.1. Note that the volt-hertz bound
    is a graph in the manuals (gap G15), so the ``2.2E7 V*Hz`` check here is
    approximate and the instrument's own fault 820 remains the authority.
    """
    problems: "list[str]" = []
    try:
        amplitude = float(amplitude)
        frequency_hz = float(frequency_hz)
    except (TypeError, ValueError):
        return [f"amplitude {amplitude!r} and frequency {frequency_hz!r} must "
                f"both be numbers"]
    if not math.isfinite(amplitude):
        problems.append(f"amplitude {amplitude!r} is not a finite number")
    if not math.isfinite(frequency_hz):
        problems.append(f"frequency {frequency_hz!r} is not a finite number")
    if problems:
        return problems

    unit_u = str(unit).strip().upper()
    if frequency_hz < 0:
        problems.append(
            f"frequency must not be negative, got {_fmt(frequency_hz)} Hz")

    if wideband:
        return problems + _validate_wideband(amplitude, unit_u, frequency_hz,
                                             range_id)

    if unit_u == "OHM":
        problems += _validate_ohms(amplitude, frequency_hz, range_id)
    elif unit_u == "V":
        if frequency_hz > 0:
            problems += _validate_ac_volts(amplitude, frequency_hz, options,
                                           range_id)
        else:
            problems += _validate_dc_volts(amplitude, range_id)
    elif unit_u == "A":
        if frequency_hz > 0:
            problems += _validate_ac_current(amplitude, frequency_hz, options,
                                             range_id)
        else:
            problems += _validate_dc_current(amplitude, options, range_id)
    else:
        problems.append(
            f"unknown unit {unit!r}: expected V, A or OHM. Never synthesize "
            f"multiplier prefixes -- 'MA' alone is milliamps but 'MAOHM' and "
            f"'MAHZ' mean mega (guide §3.6)")
    return problems


# ──────────────────────────────────────────────────────────────────────────
# Settling (guide §11.5, §8.4)
# ──────────────────────────────────────────────────────────────────────────

#: Guide §8.4 / [OM §5-68, p.5-73]: "the 5700A/5720A Series II can take up to
#: 60 ms to set the SETTLING bit". A poller that reads ``ISR?`` sooner can see
#: a stale ``SETTLED = 1`` left over from the **previous** setpoint and return
#: instantly — the silent-wrong-data failure this driver exists to avoid.
SETTLE_STATUS_LATENCY_SEC = 0.06

#: Guide §11.5 worst case across every function (ac volts, 1100 V, 5725A).
#: Also used for the resistance function, whose settling the manuals never
#: tabulate.
SETTLE_WORST_CASE_SEC = 12.0

#: Multiplier applied to :func:`expected_settle_seconds` when
#: :meth:`Fluke5700A.wait_settled` computes its own timeout. The result is
#: floored at :data:`DEFAULT_TIMEOUT_SEC` (45 s), which guide §11.5 recommends
#: for any wait after ``OUT``.
SETTLE_TIMEOUT_MARGIN = 3.0

#: Guide §8.6 / §19.2: the threshold above which the output is dangerous. It
#: is the calibrator's own line — a queued fault *silently* inhibits ``OPER``
#: for outputs at or above it — and the same number appears in
#: ``apps.fluke5700a_console``, ``apps.cal_verify`` and ``cal.procedure``
#: (as ``MAX_LIVE_OUTPUT_V``), which cannot import a driver. One test asserts
#: all four are the same number.
HIGH_VOLTAGE_V = 22.0

#: Floor for :meth:`Fluke5700A.standby`'s wait for ISR bit 0 (``OPER``) to
#: clear, and the whole deadline below :data:`HIGH_VOLTAGE_V`. ``STBY`` is an
#: *overlapped* command ([OM Table 5-2, p.190]), so the first ``ISR?`` after
#: it can still read the previous state. Long enough to out-wait the status
#: latency, short enough that a calibrator that really is ignoring ``STBY``
#: is reported at once.
STANDBY_VERIFY_TIMEOUT_SEC = 1.0

#: Ceiling for that deadline, reached at :data:`STANDBY_VERIFY_FULL_SCALE_V`.
#:
#: De-energizing is a relay, but coming *down* from a kilovolt is not: the
#: output capacitance has to bleed off before the calibrator reports the
#: output off, and a 15-point dc run ending on -1000 V was declared "the
#: output may still be live" on a run whose every point had completed, with
#: ``ISR?`` reading OPER-clear moments later. A false lethal-output warning
#: on a clean run trains the operator to distrust the next one, which may be
#: real.
#:
#: **This number is engineering judgement, not a datasheet figure.** Neither
#: the Operator's nor the Service manual tabulates how long the OPER bit
#: takes to clear. The one measured discharge figure the repo holds is the
#: A14 output capacitor — 0.72 uF at ~1100 V bled by a 600 kohm string, RC
#: ~= 0.43 s (``docs/5700a/diagnostics-decode.md``) — and 5 s is ~11.6 of
#: those time constants, far past the point where 1100 V has bled to
#: millivolts. It costs nothing on a healthy run, because the poll below
#: exits the moment the bit clears; it is only ever *spent* when the output
#: genuinely did not come off, and five seconds is still well inside the time
#: an operator needs to reach the front-panel STANDBY key.
STANDBY_VERIFY_TIMEOUT_MAX_SEC = 5.0

#: Output magnitude at which :func:`standby_verify_timeout` reaches its
#: ceiling: full scale of both volts envelopes (§11.2, ``DC1100V``/
#: ``AC1100V``).
STANDBY_VERIFY_FULL_SCALE_V = 1100.0

#: Poll interval inside that window.
STANDBY_POLL_INTERVAL_SEC = 0.05

#: Current magnitude above which :meth:`Fluke5700A.standby` takes
#: :data:`STANDBY_VERIFY_TIMEOUT_MAX_SEC` instead of the floor. It is 0:
#: **every** current output takes the ceiling.
#:
#: **Measured, not tabulated.** On one unit ``OPER`` was still set 1.0 s
#: after ``STBY`` and clear moments later at 990 mA,
#: after -1 A dc and after 0.22 A ac at 15 Hz on the 220 mA range. A first fix that kept the floor
#: up to 0.22 A failed on that last point, so no current is trusted to the
#: floor. As with the volts ramp, the long deadline is only ever spent when
#: the output genuinely did not come off.
STANDBY_HIGH_CURRENT_A = 0.0

#: An ac output below this frequency takes :data:`STANDBY_VERIFY_TIMEOUT_MAX_SEC`
#: whatever its magnitude.
#:
#: **Measured, not tabulated.** On one unit ``OPER`` was still set 1.0 s
#: after ``STBY`` following 10 V at 10 Hz (``ISR?`` read standby moments later), the same symptom
#: as 0.22 A at 15 Hz. 10 V at 1 kHz and 50 kHz came off inside
#: the floor on the same run. 100 Hz is a conservative edge between the two
#: observations, not a measured threshold.
STANDBY_LOW_FREQUENCY_HZ = 100.0

#: The shortest window :meth:`Fluke5700A.standby` ever polls for, whatever
#: :func:`standby_verify_timeout` sized.  Operator ruling 2026-11-02, after a
#: 1.0 s floor stopped a healthy adjustment mid-group: "let it retry for at
#: least 10 seconds".  The poll exits the moment ``OPER`` clears, so this is
#: only ever spent on an output that really did not come off -- and 10 s is
#: still well inside the time an operator needs to reach the STANDBY key.
STANDBY_RETRY_MIN_SEC = 10.0


def standby_verify_timeout(volts: Optional[float],
                           amps: Optional[float] = 0.0, *,
                           low_frequency: bool = False) -> float:
    """How long to wait for ``OPER`` to clear after ``STBY``. **No I/O.**

    *volts* is the magnitude, in volts, of the largest output **energized**
    since the last verified de-energize — 0 for a function that puts no volts
    on the terminals (amps, ohms), and ``None`` when the driver cannot know,
    which is the case until it has itself staged and energized a setpoint.

    Below :data:`HIGH_VOLTAGE_V` the deadline is exactly what it always was,
    :data:`STANDBY_VERIFY_TIMEOUT_SEC`: a 0.1 V point that has not come off
    in a second has not come off at all, and nothing about low-voltage
    behaviour changes. From there it ramps linearly to
    :data:`STANDBY_VERIFY_TIMEOUT_MAX_SEC` at
    :data:`STANDBY_VERIFY_FULL_SCALE_V` and is clamped there.

    *amps* is the same for current: the largest current energized since the
    last verified de-energize, 0 for a volts or ohms output. Above
    :data:`STANDBY_HIGH_CURRENT_A` (0: any current at all) it takes the
    ceiling, because de-energizing ~1 A dc and 0.22 A ac has been measured
    to take longer than the floor.

    *low_frequency* is true when an ac output below
    :data:`STANDBY_LOW_FREQUENCY_HZ` was energized since the last verified
    de-energize; it takes the ceiling too (10 V at 10 Hz measured slow).

    ``None`` takes the ceiling rather than the floor, and that asymmetry is
    deliberate. The deadline is only ever reached when the output did **not**
    come off, so being generous costs a few seconds on the failure path and
    nothing at all on the healthy one; being stingy costs a false "the output
    must be assumed live" on a run that was fine. Where the driver does not
    know what is on the terminals — a front-panel setpoint, an ``INCR``/
    ``MULT`` edit, a ``CAL_RNG`` staged output — it assumes the worst.
    """
    if volts is None or amps is None:
        return STANDBY_VERIFY_TIMEOUT_MAX_SEC
    try:
        magnitude = abs(float(volts))
        current = abs(float(amps))
    except (TypeError, ValueError):
        return STANDBY_VERIFY_TIMEOUT_MAX_SEC
    if not (math.isfinite(magnitude) and math.isfinite(current)):
        return STANDBY_VERIFY_TIMEOUT_MAX_SEC
    if current > STANDBY_HIGH_CURRENT_A:
        # See STANDBY_HIGH_CURRENT_A: current outputs come off slowly.
        return STANDBY_VERIFY_TIMEOUT_MAX_SEC
    if low_frequency and magnitude > 0.0:
        # See STANDBY_LOW_FREQUENCY_HZ: low-frequency ac comes off slowly.
        return STANDBY_VERIFY_TIMEOUT_MAX_SEC
    if magnitude < HIGH_VOLTAGE_V:
        return STANDBY_VERIFY_TIMEOUT_SEC
    if magnitude >= STANDBY_VERIFY_FULL_SCALE_V:
        return STANDBY_VERIFY_TIMEOUT_MAX_SEC
    fraction = ((magnitude - HIGH_VOLTAGE_V)
                / (STANDBY_VERIFY_FULL_SCALE_V - HIGH_VOLTAGE_V))
    return (STANDBY_VERIFY_TIMEOUT_SEC
            + fraction * (STANDBY_VERIFY_TIMEOUT_MAX_SEC
                          - STANDBY_VERIFY_TIMEOUT_SEC))


def expected_settle_seconds(unit: str, frequency_hz: float, *,
                            range_change: bool = False,
                            polarity_change: bool = False,
                            range_id: Optional[str] = None) -> float:
    """Settling time to full accuracy, from guide §11.5. **No I/O.**

    Every range-dependent term — the dc/ac 1100 V adders, the dc-current base
    figure, the 5725A adders, the wideband function — is keyed on *range_id*,
    the §10 identifier, so pass one: ``expected_range_id()`` predicts it and
    ``RANGE?`` reports the real one. The 5725A adders deliberately key on the
    amplifier *range* (``AC5725V``, ``AC5725A``, ``DC5725A``), not on
    attachment: an attached amplifier that is not sourcing adds nothing.

    There is deliberately **no** ``options`` parameter, unlike
    :func:`validate_output`. A capability string must never be able to move a
    settling number — a unit that merely has a wideband module installed still
    settles as standard ac volts while it is sourcing standard ac volts — and
    a ``TypeError`` at the call site enforces that better than a docstring.

    Where a range is needed and none is given, the **worst** base-instrument
    figure is used (dc current: 3 s, the 2.2 A figure), because this value
    feeds a timeout and optimism there turns a slow setpoint into a spurious
    failure. Range-keyed *adders*, by contrast, default to absent.
    """
    unit_u = str(unit).strip().upper()
    range_u = str(range_id).strip().upper() if range_id else ""

    if unit_u == "V":
        if range_u.startswith("WB"):
            # Wideband ac (Option 5700A-03), guide §11.5.
            if frequency_hz < 120.0:
                return 7.0
            if _le(frequency_hz, 1.2e6):
                return 5.0
            return 0.5
        if frequency_hz > 0:
            if frequency_hz < 20.0:
                seconds = 7.0
            elif frequency_hz < 120.0:
                # The 20 Hz - 120 Hz band is *not* covered by the manual's
                # settling table (guide §11.5, §18.4). Use the 7 s worst case.
                seconds = 7.0
            elif _le(frequency_hz, 120.0e3):
                seconds = 5.0
            else:
                seconds = 2.0
            if range_change:
                seconds += 1.0
            if range_u == "AC5725V":
                seconds += 4.0
            elif range_u == "AC1100V":
                seconds += 2.0
            return seconds
        seconds = 3.0
        if range_change or polarity_change:
            seconds += 1.0
        if range_u == "DC1100V":
            seconds += 1.0
        return seconds

    if unit_u == "A":
        if frequency_hz > 0:
            seconds = 6.0 if range_u == "AC5725A" else 5.0
            if range_change:
                seconds += 1.0
            return seconds
        if range_u == "DC5725A":
            seconds = 6.0
        elif range_u in ("DC220UA", "DC2_2MA", "DC22MA", "DC220MA"):
            seconds = 1.0
        else:
            # DC2_2A, or no range given: the worst base-instrument figure.
            seconds = 3.0
        if range_change or polarity_change:
            seconds += 1.0
        return seconds

    if unit_u == "OHM":
        # Guide §11.5 has no resistance row; use the overall worst case.
        return SETTLE_WORST_CASE_SEC

    raise ValueError(f"unknown unit {unit!r}: expected V, A or OHM")


# ──────────────────────────────────────────────────────────────────────────
# Long-term commands, diagnostics and calibration (guide §7.4-§7.6, §9,
# §14.6-§14.11, §16)
# ──────────────────────────────────────────────────────────────────────────

#: The six long-term commands (guide §7.4). While one of these is running,
#: **any** state-changing command is rejected with fault
#: :data:`FAULT_LONG_TERM_IN_PROGRESS`.
LONG_TERM_COMMANDS = frozenset({
    "CAL_ADJ", "CAL_CHK", "CAL_REF", "CAL_WBGAIN", "CAL_ZERO", "DIAG",
})

#: Poll period for :meth:`Fluke5700A._run_long_term`. One second: these
#: commands run for minutes, and each poll costs two queries on a bus shared
#: with the sampling DMMs.
LONG_TERM_POLL_INTERVAL_SEC = 1.0

#: How many **consecutive** failed ``STATE?`` progress polls
#: :meth:`Fluke5700A._run_long_term` tolerates before giving up. A progress
#: poll is decoration; it must not be able to abandon a 30-minute ``CAL_ADJ``
#: — least of all on the sick unit this driver was written for, whose
#: ``EXPLAIN?`` is already unreliable enough to need a latched offline
#: fallback. The bound is what still surfaces a genuinely dead link.
MAX_PROGRESS_POLL_FAILURES = 5

#: Default timeout for a long-term command whose duration the manuals never
#: state (gap G11 for ``DIAG``). 30 minutes is a guess chosen to be generous,
#: **not** a specification. Measure the real figure on first use and pass it
#: explicitly.
#:
#: Measured on a 5700A Series II (firmware 1.6) 2026-08-14:
#: ``DIAG D5700`` takes **603 s** with A5/A6 absent — the wideband tests are
#: skipped — and **149 s** when A12/A13 are also out, because the missing
#: oscillator truncates the run before the analog phases.
LONG_TERM_TIMEOUT_SEC = 1800.0

#: Default timeout for ``CAL_CHK``. The Series II Operators Manual is explicit
#: where the programming guide was not (gap G10, now closed): "Checking the
#: calibration **takes about an hour**" ([OM §1-19], and §7-12 for the
#: procedure). 30 minutes — the generic long-term default — is therefore *not*
#: enough, and the failure is nasty: the read times out while the calibration
#: check keeps running, and there is no remote abort to stop it.
#:
#: 90 minutes gives half again over the stated hour. A unit with the wideband
#: option absent has fewer points to check and should finish sooner.
CAL_CHK_TIMEOUT_SEC = 5400.0

#: Default timeout for ``CAL_ZERO``. The manual gives ≈2½ minutes, plus 30 s
#: with a 5725A attached (guide §9 ``CAL_ZERO``); 420 s is that with a wide
#: margin, and satisfies the ">= 5 min" floor.
CAL_ZERO_TIMEOUT_SEC = 420.0

#: Default read timeout for ``*TST?``. It is a **blocking** query with no
#: documented duration and no non-blocking form (gap G9), so this is a guess:
#: measure it on the first run and record the figure.
SELF_TEST_TIMEOUT_SEC = 600.0

#: Read timeout used when probing an uncertainly spelled *query*. Gap G31: an
#: unknown query produces **no response at all**, so the probe can only fail
#: by timing out — it must therefore be short, or every probe costs a full
#: session timeout.
PROBE_TIMEOUT_SEC = 2.0

#: ``DIAG`` parameters (guide §9 ``DIAG``). ``CONT`` and ``ABORT`` are the
#: stepping controls used with ``DIAGFLT HALT``.
DIAG_TARGETS = ("ALL", "D5700", "DV5725", "DI5725", "CONT", "ABORT")

#: ``DIAGFLT`` parameters. **Stored in nonvolatile memory**; the factory
#: default is ``ABORT`` (guide §9 ``DIAGFLT``).
DIAGFLT_MODES = ("HALT", "ABORT", "CONT")
DIAGFLT_DEFAULT = "ABORT"

#: Constant-group selectors for ``CAL_CLST?`` / ``CAL_CONST?`` / ``OHMSREF?``.
CAL_CONSTANT_GROUPS = ("CAL", "CHECK", "PREV")

#: Selectors for ``CAL_SLST?`` / ``CAL_SHIFT?`` — no ``PREV`` here.
CAL_SHIFT_GROUPS = ("CAL", "CHECK")

#: Selectors for ``CAL_RPT?``.
CAL_REPORT_GROUPS = ("CAL", "CHECK", "RAW")

#: Activity selectors for ``CAL_DATE?`` / ``CAL_DAYS?``.
CAL_ACTIVITIES = ("B5725", "CAL", "WBFLAT", "WBGAIN", "ZERO")

#: Activity selectors for ``CAL_TEMP?`` — note ``CHECK`` where
#: ``CAL_DATE?``/``CAL_DAYS?`` have ``ZERO`` (guide §9 ``CAL_TEMP?``). The two
#: lists are deliberately separate constants because that difference is a
#: documented trap, not a typo.
CAL_TEMP_ACTIVITIES = ("B5725", "CAL", "WBFLAT", "WBGAIN", "CHECK")

#: ``CAL_INTV`` values, in days.
CAL_INTERVALS_DAYS = (1, 90, 180, 365)

#: ``CAL_CONF`` values. Guide §13 item 5: the OM spells them ``CONF95`` /
#: ``CONF99`` and the RPR spells them ``95`` / ``99``; the OM form is
#: recommended because it matches what ``CAL_CONF?`` answers.
CAL_CONF_LEVELS = ("CONF95", "CONF99")

#: ``DATEFMT`` values.
DATE_FORMATS = ("MDY", "DMY", "YMD")

#: ``CAL_REF`` accepts only the four artifact steps of guide §14.6, whose
#: units are volts, kilohms and ohms. Multipliers are never *synthesized* by
#: this driver (rule 5), but ``KOHM`` is the unit the manual's own 10 kΩ step
#: is written with and the parameter's units select the procedure, so it is
#: accepted verbatim when the caller asks for it.
CAL_REF_UNITS = ("V", "OHM", "KOHM")

#: ``RPT_STR`` limit (guide §9; fault 844 "Report String Too Long").
RPT_STR_MAX_CHARS = 40

#: ``*PUD`` limit — "allowing up to 63 bytes" (guide §9 ``*PUD``).
PUD_MAX_BYTES = 63

#: Warm-up requirement: "twice the time since last warmed up, to a maximum of
#: 30 minutes" (guide §11.6). Check with :meth:`Fluke5700A.ontime`.
WARMUP_MAX_MINUTES = 30

#: Calibration is specified only over 15-35 °C (guide §11.6); the operating
#: range is 0-50 °C. :meth:`Fluke5700A.cal_temp` refuses anything outside the
#: **operating** range — that catches a `230` typed for `23.0` — and documents
#: the narrower specified band without enforcing it.
CAL_TEMP_SPEC_MIN_C = 15.0
CAL_TEMP_SPEC_MAX_C = 35.0
CAL_TEMP_OPERATING_MIN_C = 0.0
CAL_TEMP_OPERATING_MAX_C = 50.0

#: ``CAL_RNG`` magnitude window: "Must be 45 % to 95 % of the specified range"
#: (guide §9 ``CAL_RNG``, §14.8); faults 847 / 848 otherwise.
CAL_RNG_MIN_FRACTION = 0.45
CAL_RNG_MAX_FRACTION = 0.95

#: Amplifier-suffixed §10 range identifiers. They exist only when the matching
#: amplifier is selected with ``BTYPE`` and attached, and this module has no
#: full-scale figure for the 5205A / 5215A / 5220A, so they are listed for
#: recognition only.
AMPLIFIER_RANGE_IDS = frozenset({
    "DC5205V", "AC5205V", "AC5215V", "DC5220A", "AC5220A",
    "DC5725A", "AC5725A", "AC5725V",
})

#: Full-scale magnitude per §10 range identifier, for the ranges this module
#: tabulates (guide §11.2, §18.1). Resistance and amplifier ranges are absent
#: on purpose — see :func:`validate_cal_rng`.
RANGE_FULL_SCALE = {
    entry.range_id: entry.full_scale
    for table in (DC_VOLTS_RANGES, AC_VOLTS_RANGES, DC_CURRENT_RANGES,
                  AC_CURRENT_RANGES, WIDEBAND_RANGES)
    for entry in table
}

#: Every §10 range identifier, as corrected by guide §10 (``DC220MA`` for the
#: manual's ``DC 220A``, ``OHM1_9K`` for its duplicated ``OHM19K``). Used to
#: recognize a ``CAL_RNG`` / ``CAL_SHIFT?`` range argument. Guide §10 still
#: recommends enumerating the true set from the firmware at run time
#: (``CAL_SLST? CAL`` emits the range names itself).
RANGE_IDS = (frozenset(RANGE_FULL_SCALE)
             | RESISTANCE_RANGE_IDS | AMPLIFIER_RANGE_IDS)


def cal_rng_unit(range_id: str) -> str:
    """Base unit to send with a ``CAL_RNG`` magnitude for *range_id*.

    Derived from the §10 identifier rather than looked up, because every
    identifier ends in its unit letter: ``DC220MV``/``WB3_3MV``/``AC5725V``
    are volts, ``DC220UA``/``AC2_2A``/``DC5725A`` are amps, and anything
    starting ``OHM`` is ohms.
    """
    rid = str(range_id).strip().upper()
    if rid.startswith("OHM"):
        return "OHM"
    if rid.endswith("V"):
        return "V"
    if rid.endswith("A"):
        return "A"
    raise ValueError(
        f"cannot infer a unit from range identifier {range_id!r}; expected a "
        f"guide §10 identifier ending in V or A, or starting with OHM")


def validate_cal_rng(range_id: str, magnitude: float) -> "list[str]":
    """Check a ``CAL_RNG`` magnitude offline. **No I/O.**

    Returns a list of human-readable violations; empty means it is inside the
    45 %-95 % window of guide §9 ``CAL_RNG`` (faults 847 / 848).

    The check is skipped — and an empty list returned — for range identifiers
    whose full scale this module does not tabulate: the **resistance** ranges,
    where the range *is* a single discrete value so a percentage of it is
    meaningless, and the **amplifier** ranges (``DC5205V`` and friends), whose
    envelopes are not in the guide. Refusing them outright would block a
    legitimate service action; the instrument's own faults 847/848 remain the
    authority there.
    """
    rid = str(range_id).strip().upper()
    if rid not in RANGE_IDS:
        return [f"unknown range identifier {range_id!r}; expected one of the "
                f"guide §10 identifiers, or the keywords NULL or STORE"]
    full_scale = RANGE_FULL_SCALE.get(rid)
    if full_scale is None:
        return []
    try:
        magnitude = float(magnitude)
    except (TypeError, ValueError):
        return [f"CAL_RNG magnitude {magnitude!r} is not a number"]
    if not math.isfinite(magnitude):
        return [f"CAL_RNG magnitude {magnitude!r} is not a finite number"]
    low = CAL_RNG_MIN_FRACTION * full_scale
    high = CAL_RNG_MAX_FRACTION * full_scale
    value = abs(magnitude)
    if not _ge(value, low):
        return [f"{_fmt(value)} is below {CAL_RNG_MIN_FRACTION:.0%} of the "
                f"{_fmt(full_scale)} {rid} range ({_fmt(low)}); a range "
                f"calibration magnitude must be "
                f"{CAL_RNG_MIN_FRACTION:.0%}-{CAL_RNG_MAX_FRACTION:.0%} of "
                f"range (fault 848)"]
    if not _le(value, high):
        return [f"{_fmt(value)} is above {CAL_RNG_MAX_FRACTION:.0%} of the "
                f"{_fmt(full_scale)} {rid} range ({_fmt(high)}); a range "
                f"calibration magnitude must be "
                f"{CAL_RNG_MIN_FRACTION:.0%}-{CAL_RNG_MAX_FRACTION:.0%} of "
                f"range (fault 847)"]
    return []


def _one_of(value, allowed: Sequence[str], what: str) -> str:
    """Uppercase *value* and require it to be in *allowed*."""
    token = str(value).strip().upper()
    if token not in allowed:
        raise ValueError(
            f"{what} must be one of {', '.join(allowed)}; got {value!r}")
    return token


# ──────────────────────────────────────────────────────────────────────────
# Parsers for the calibration / housekeeping responses
# ──────────────────────────────────────────────────────────────────────────

def _six_digits(value, what: str, resp: str) -> str:
    """Normalize a zero-padded 6-digit ``HHMMSS`` / date field.

    Guide §9 ``CLOCK?`` and gap G25: these fields are typed ``(Integer)`` but
    are really zero-padded 6-digit strings — 08:00:00 comes back as ``80000``.
    Parsing them as bare integers loses the leading zero.
    """
    text = _unquote(str(value).strip())
    if text.startswith("+"):
        text = text[1:]
    if not text.isdigit():
        raise Fluke5700AProtocolError(
            f"{what}: {value!r} is not a 6-digit field in {resp!r}", resp)
    if len(text) > 6:
        raise Fluke5700AProtocolError(
            f"{what}: {value!r} has more than 6 digits in {resp!r}", resp)
    return text.zfill(6)


def parse_clock(resp: str) -> "tuple[str, str]":
    """Parse ``CLOCK?`` into ``(hhmmss, date)``, both zero-padded to 6 digits.

    The date's field order follows ``DATEFMT`` (``MMDDYY`` / ``DDMMYY`` /
    ``YYMMDD``), which is why it is returned as digits rather than as a
    ``date``: guide §9 says to query ``DATEFMT?`` before interpreting it, and
    gap G25 notes there is no century window for the two-digit year.
    """
    time_field, date_field = _fields(resp, 2, "CLOCK?")
    return (_six_digits(time_field, "CLOCK? time", resp),
            _six_digits(date_field, "CLOCK? date", resp))


def parse_pud(resp: str) -> str:
    """Parse a ``*PUD?`` response into the stored user data.

    Encoding (guide §9 ``*PUD?``): ``#<n><digits><user data>``, where ``n`` is
    a single digit giving the length of the ``<digits>`` field and
    ``<digits>`` is the byte count of the payload. The manual's own example
    prints ``205FLUKE`` with the leading ``#`` missing (§13 item 22), so the
    ``#`` is optional here. An empty response means empty memory.
    """
    text = _unquote(resp).strip()
    if not text:
        return ""
    if text.startswith("#"):
        text = text[1:]
    if not text:
        return ""
    if not text[0].isdigit():
        raise Fluke5700AProtocolError(
            f"*PUD?: expected a digit count, got {resp!r}", resp)
    width = int(text[0])
    digits = text[1:1 + width]
    if len(digits) != width or not digits.isdigit():
        raise Fluke5700AProtocolError(
            f"*PUD?: malformed length field in {resp!r}", resp)
    length = int(digits)
    payload = text[1 + width:]
    if len(payload) < length:
        raise Fluke5700AProtocolError(
            f"*PUD?: declared {length} bytes but only {len(payload)} "
            f"followed in {resp!r}", resp)
    return payload[:length]


@dataclass(frozen=True)
class ShiftPoint:
    """One record of a ``CAL_SHIFT?`` / ``CAL_SLST?`` block (guide §9).

    Magnitude and the two absolute shifts are in the range's own units
    (volts for every dc-volts range, and so on); *relative_shift_ppm* and
    *spec_ppm* are in ppm and *percent_of_spec* in percent.
    """

    magnitude: float
    frequency_hz: float
    offset: float
    absolute_shift: float
    relative_shift_ppm: float
    percent_of_spec: float
    spec_ppm: float


@dataclass(frozen=True)
class RangeShift:
    """The ``CAL_SHIFT?`` record set for one §10 range."""

    range_id: str
    points: "tuple[ShiftPoint, ...]"


def _parse_shift_ranges(lines: Sequence[str]) -> "list[RangeShift]":
    """Parse ``<range>,<n>`` headers each followed by *n* 7-field records."""
    ranges: "list[RangeShift]" = []
    index = 0
    while index < len(lines):
        header = lines[index]
        fields = [f.strip() for f in _unquote(header).split(",")]
        if len(fields) != 2:
            raise Fluke5700AProtocolError(
                f"CAL_SHIFT?: expected a '<range id>,<point count>' header, "
                f"got {header!r}", header)
        range_id = _unquote(fields[0]).upper()
        count = _to_int(fields[1], header, "CAL_SHIFT? point count")
        index += 1
        points: "list[ShiftPoint]" = []
        for _ in range(count):
            if index >= len(lines):
                raise Fluke5700AProtocolError(
                    f"CAL_SHIFT?: range {range_id} declared {count} points "
                    f"but the block ended after {len(points)}", header)
            row = lines[index]
            values = [f.strip() for f in _unquote(row).split(",")]
            if len(values) != 7:
                raise Fluke5700AProtocolError(
                    f"CAL_SHIFT?: expected 7 comma-separated fields, got "
                    f"{len(values)} in {row!r}", row)
            points.append(ShiftPoint(*(
                _to_float(value, row, "CAL_SHIFT? field")
                for value in values)))
            index += 1
        ranges.append(RangeShift(range_id, tuple(points)))
    return ranges


def parse_range_shift(lines: Sequence[str]) -> RangeShift:
    """Parse a ``CAL_SHIFT? <group>, <range>`` block (one range)."""
    ranges = _parse_shift_ranges(lines)
    if len(ranges) != 1:
        raise Fluke5700AProtocolError(
            f"CAL_SHIFT?: expected exactly one range block, got "
            f"{len(ranges)}", "\n".join(lines))
    return ranges[0]


def parse_shift_list(lines: Sequence[str]) -> "list[RangeShift]":
    """Parse a ``CAL_SLST? <group>`` block (a count, then every range).

    The leading count's meaning is not stated — the manual's example prints
    ``49`` above range blocks of two points each, which fits neither reading
    obviously — so it is accepted as either the number of ranges or the total
    number of points. Anything else raises: a block truncated by a read
    timeout must never be mistaken for a complete one.
    """
    if not lines:
        raise Fluke5700AProtocolError("CAL_SLST?: empty block", "")
    declared = _to_int(_unquote(lines[0]).strip(), lines[0],
                       "CAL_SLST? count")
    ranges = _parse_shift_ranges(lines[1:])
    total_points = sum(len(entry.points) for entry in ranges)
    if declared not in (len(ranges), total_points):
        raise Fluke5700AProtocolError(
            f"CAL_SLST?: block declares {declared} but carries "
            f"{len(ranges)} ranges / {total_points} points; refusing to "
            f"return a possibly truncated block", lines[0])
    return ranges


def parse_constant_list(lines: Sequence[str]) -> "dict[str, float]":
    """Parse a ``CAL_CLST? <group>`` block into ``{name: value}``.

    Layout (guide §9 ``CAL_CLST?``): a total count, then one ``<name>,
    <value>`` pair per line. The OM's formal template shows the pair
    whitespace-separated while its own example shows it comma-separated, so
    **both** are accepted, as the guide directs.

    The declared count *is* enforced here — unlike ``CAL_SLST?``'s, its
    meaning is unambiguous ("total number of constants") — because a ≈400-line
    block cut short by a read timeout would otherwise look complete. Duplicate
    names raise for the same reason: the dict would silently lose one.
    """
    if not lines:
        raise Fluke5700AProtocolError("CAL_CLST?: empty block", "")
    declared = _to_int(_unquote(lines[0]).strip(), lines[0],
                       "CAL_CLST? count")
    constants: "dict[str, float]" = {}
    for row in lines[1:]:
        text = _unquote(row).strip()
        if not text:
            continue
        if "," in text:
            name, _, value = text.partition(",")
        else:
            parts = text.split(None, 1)
            if len(parts) != 2:
                raise Fluke5700AProtocolError(
                    f"CAL_CLST?: expected '<name>,<value>' or "
                    f"'<name> <value>', got {row!r}", row)
            name, value = parts
        name = _unquote(name.strip())
        if name in constants:
            raise Fluke5700AProtocolError(
                f"CAL_CLST?: constant {name!r} appears twice", row)
        constants[name] = _to_float(value.strip(), row,
                                    f"CAL_CLST? value for {name}")
    if len(constants) != declared:
        raise Fluke5700AProtocolError(
            f"CAL_CLST?: block declares {declared} constants but carries "
            f"{len(constants)}; refusing to return a possibly truncated "
            f"block", lines[0])
    return constants


# ──────────────────────────────────────────────────────────────────────────
# Driver
# ──────────────────────────────────────────────────────────────────────────

class Fluke5700A:
    """Fluke 5700A / 5720A Series II calibrator over IEEE-488 or RS-232C.

    The session is opened in ``__init__`` (guide §14.1) so that a constructed
    object is always a live, verified, standby-state calibrator. Use it as a
    context manager to get the §14.4 safe shutdown on every exit path.

    Pass *serial_port* to use the rear-panel RS-232C port instead of GPIB::

        with Fluke5700A(serial_port="/dev/ttyUSB0") as cal:
            ...

    Everything above the transport is the same on both interfaces; what
    differs is confined to :class:`SerialTransport` and to the extra opening
    steps documented on :meth:`open`.

    Attributes set by :meth:`open`:

    ``identity``        parsed ``*IDN?`` (:class:`Identity`)
    ``options``         ``*OPT?`` strings, e.g. ``["5725A Attached"]``
    ``startup_faults``  whatever was sitting in the fault queue at open
    ``serial_setup``    parsed ``SP_SET?`` (:class:`SerialSetup`); ``None`` on
                        a GPIB session, which has no such settings

    Set *service* to allow the guide §16 service-only commands
    (:meth:`fatality`, :meth:`fatalclr`, :meth:`set_etime`); they refuse to
    run otherwise.
    """

    def __init__(self, gpib_address: int = DEFAULT_GPIB_ADDRESS,
                 gpib_bus: int = DEFAULT_GPIB_BUS, *,
                 serial_port: Optional[str] = None,
                 serial_baud: int = DEFAULT_SERIAL_BAUD,
                 transport: Optional[Transport] = None,
                 timeout: float = DEFAULT_TIMEOUT_SEC,
                 name: Optional[str] = None,
                 srq: bool = False,
                 service: bool = False):
        # True on an RS-232 link; see :class:`Transport`. Consulted by open()
        # and by the "not in Remote" diagnosis, both of which would otherwise
        # give advice about a REN line that does not exist over serial.
        # Settled *before* the port is opened, so a refusal below leaks
        # nothing.
        self.is_serial = (serial_port is not None if transport is None
                          else bool(getattr(transport, "is_serial", False)))
        if srq and self.is_serial:
            raise ValueError(
                "srq=True is meaningless over RS-232: the calibrator has no "
                "SRQ capability on the serial port (guide §2.5 item 5). The "
                "status registers still work — poll ISR?/*STB? instead.")
        if transport is None:
            if serial_port is not None:
                transport = SerialTransport(serial_port, serial_baud,
                                            timeout=timeout)
            else:
                transport = GpibTransport(gpib_address, gpib_bus,
                                          timeout=timeout)
        self._t = transport
        # No MultimeterBase to inherit ``name`` from; it is a plain attribute
        # and, as everywhere in caldmm, an operator-chosen designator that
        # says nothing about the instrument's role.
        self.name = name
        self.timeout = timeout
        self.srq = srq
        # Gate for the guide §16 service-only commands (FATALITY?, FATALCLR,
        # ETIME SET_TO). Off by default: they are not part of the operator
        # command set and the calibrator answers fault 2217 ("Command Reserved
        # For Service") to them on some firmware.
        self.service = service
        self.identity: Optional[Identity] = None
        self.options: "list[str]" = []
        # Parsed SP_SET? for a serial session; None over GPIB, which has no
        # port settings of its own.
        self.serial_setup: Optional[SerialSetup] = None
        # STATE? code the last diag() run ended on; None until one has run.
        # diag_halted() reports from this rather than re-querying.
        self.last_diag_state: Optional[int] = None
        self.startup_faults: "list[tuple[int, str]]" = []
        self.shutdown_faults: "list[tuple[int, str]]" = []
        # Why the last :meth:`_safe_standby` could not prove the output came
        # off, or None when it could. Kept because that path may not raise:
        # see :meth:`_safe_standby`.
        self.standby_error: Optional[BaseException] = None
        # Cached RLOCK state. ``*RST`` in open() unlocks the range, but the
        # front panel can lock it behind the driver's back, so
        # ``is_range_locked()`` re-reads ISR? rather than trusting this.
        self._range_locked = False
        # Cached WBAND state, same pattern: ``*RST`` turns wideband off, the
        # front-panel W BND key can turn it on behind the driver's back, and
        # ``is_wideband()`` re-reads ISR?. The cache decides which envelope
        # ``set_output`` validates against.
        self._wideband = False
        # Settling estimate for the last setpoint, used as wait_settled()'s
        # default timeout. None until the first set_output().
        self._settle_estimate: Optional[float] = None
        # ``(amplitude, unit)`` of the last setpoint this driver staged, or
        # None when it cannot know -- before the first set_output(), and
        # after an INCR/MULT edit or a CAL_RNG that moved the output out from
        # under it. Only ever read for its magnitude in volts.
        self._staged_output: Optional["tuple[float, str]"] = None
        # Magnitude in volts of the largest output *energized* since the last
        # verified de-energize; 0.0 for "nothing live", None for "unknown".
        # standby() sizes its OPER-bit deadline from this, because coming
        # down from a kilovolt is not the same wait as coming down from
        # 100 mV. It starts unknown -- a session may open onto a calibrator
        # the front panel left in OPERATE at full scale -- and open() lowers
        # it to 0.0 when the ISR? word it reads says the output is off.
        self._live_volts: Optional[float] = None
        # The same for current, in amps: the 2.2 A range comes off slower
        # than the floor (STANDBY_HIGH_CURRENT_A). Unknown is carried by
        # _live_volts being None, which already takes the ceiling.
        self._live_amps: float = 0.0
        # Frequency of the staged setpoint (0 for dc/ohms), and whether an ac
        # output below STANDBY_LOW_FREQUENCY_HZ has been energized since the
        # last verified de-energize: low-frequency ac comes off slowly.
        self._staged_hz: float = 0.0
        self._live_low_freq: bool = False
        try:
            self.open()
        except Exception:
            # Release the descriptor, but do *not* run the §14.4 shutdown
            # sequence: open() may have failed precisely because something
            # other than a calibrator is answering at this address, and it
            # must not be sent STBY/*CLS.
            try:
                self._t.close()
            except Exception:
                pass
            raise

    # ── low-level I/O ─────────────────────────────────────────────────

    def cmd(self, cmd_str: str) -> None:
        """Send a command, expecting no response."""
        try:
            self._t.write(cmd_str)
        except Fluke5700AError:
            raise
        except Exception as exc:
            # Broad on purpose: ``gpib.GpibError`` derives from neither
            # OSError nor TimeoutError, so there is no narrower common base.
            raise Fluke5700ACommError(
                f"write failed for {cmd_str!r}: {exc}") from exc

    def _query_raw(self, cmd_str: str) -> str:
        """Query, normalising transport failures, but allowing an empty reply.

        Separated from :meth:`query` so that a caller which has a legitimate
        reason to accept a blank response can still tell a blank response apart
        from a dead link — :meth:`query` reports both as
        :class:`Fluke5700ACommError`.
        """
        try:
            resp = self._t.query(cmd_str)
        except Fluke5700AError:
            raise
        except Exception as exc:
            raise Fluke5700ACommError(
                f"query failed for {cmd_str!r}: {exc}") from exc
        return (resp or "").strip()

    def query(self, cmd_str: str) -> str:
        """Send a query and return its (stripped) single-line response."""
        resp = self._query_raw(cmd_str)
        if not resp:
            raise Fluke5700ACommError(
                f"empty response to {cmd_str!r} (timeout or link down)")
        return resp

    def query_int(self, cmd_str: str) -> int:
        """Send a query and parse its response as an integer."""
        resp = self.query(cmd_str)
        return _to_int(_unquote(resp), resp, f"{cmd_str} response")

    def serial_poll(self) -> int:
        """Hardware serial poll — returns the STB and clears RQS/SRQ.

        Preferred over ``*STB?`` on GPIB: ``*STB?`` leaves RQS latched
        (guide §8.2, §9 ``*STB?``).
        """
        try:
            return int(self._t.serial_poll())
        except Fluke5700AError:
            raise
        except Exception as exc:
            raise Fluke5700ACommError(f"serial poll failed: {exc}") from exc

    def read_block(self, cmd_str: str, **kwargs) -> "list[str]":
        """Issue *cmd_str* and read its multi-line block response.

        The safe form of :func:`read_block`: it writes the query first, and
        restores the **session** timeout afterwards, so the short per-line
        timeout the block reader needs cannot leak out and make a later slow
        operation (an ``*OPC?`` after a long-term command) fail spuriously.
        Any keyword accepted by :func:`read_block` may be overridden.

        The raw transport is passed through deliberately — see
        :func:`read_block` on why it must not be given a wrapped reader.
        """
        kwargs.setdefault("restore_timeout", self.timeout)
        self.cmd(cmd_str)
        return read_block(self._t, **kwargs)

    # ── fault queue (guide §8.5, §8.6, §14.3) ─────────────────────────

    def drain_faults(self, *, max_iterations: int = 20
                     ) -> "list[tuple[int, str]]":
        """Read the fault queue empty and return ``(code, explanation)`` pairs.

        Repeats ``FAULT?`` until it answers ``0`` (guide §8.5) and decodes each
        non-zero code with ``EXPLAIN?``, falling back to the offline
        :data:`FAULT_CODES` table when that query fails or comes back empty.
        Surrounding quotes are stripped defensively — ``EXPLAIN?`` is listed
        among the quoted responses but its own example is unquoted
        (guide §13 item 31).

        Draining is **not optional**: guide §8.6 says ``OPER`` is silently
        inhibited for outputs >= 22 V while the queue is non-empty. Draining
        (rather than ``*CLS``) is the recommended way to clear the inhibit
        because it preserves the fault information.

        The queue holds 16 entries, the 16th always being "queue overflow", so
        a healthy instrument empties well inside *max_iterations*. A sick one
        that never answers ``0`` must not hang the caller: on hitting the
        bound this returns what it collected rather than raising.

        The iteration bound alone does not bound the *wall time*, because each
        ``EXPLAIN?`` is issued at the full session timeout. A unit that answers
        ``FAULT?`` but not ``EXPLAIN?`` — an entirely plausible state for the
        faulty-A14 unit this driver was written for — would otherwise stall
        ``open()`` for ``max_iterations`` × 45 s and look like a hang. So the
        offline fallback is **latched**: after the first ``EXPLAIN?`` failure,
        the rest of the drain decodes from :data:`FAULT_CODES` without going
        back to the instrument.

        This method never raises :class:`CalibratorFault` — it reports. A
        raising wrapper belongs with the output-control API.
        """
        codes: "list[tuple[int, str]]" = []
        explain_works = True
        for _ in range(max_iterations):
            code = self.query_int("FAULT?")
            if code == 0:
                break
            text = self._explain(code) if explain_works else None
            if text is None:
                explain_works = False
                text = explain_fault(code)
            codes.append((code, text))
        return codes

    def _explain(self, code: int) -> Optional[str]:
        """``EXPLAIN? <code>``; ``None`` if it failed or answered empty."""
        try:
            text = _unquote(self.query(f"EXPLAIN? {code}"))
        except Fluke5700AError:
            return None
        return text or None

    # ── session (guide §14.1, §14.4) ──────────────────────────────────

    def open(self) -> None:
        """Bring the session up per guide §14.1.

        Order matters and is asserted by the tests:

        1. set the controller timeout (>= 45 s, guide §11.5),
        2. Device Clear — clears the I/O buffers (guide §3.10),
        2a. **serial only**: ``REMOTE``, then ``SP_SET COMP`` and a
           ``SP_SET?`` verification — see :meth:`_open_serial_port`,
        3. ``*CLS`` — clears ESR, ISCR, fault queue and RQS (guide §8),
        4. drain the fault queue (belt and braces, guide §8.6),
        5. ``*IDN?`` — verify this really is a Fluke calibrator,
        6. ``*OPT?`` — discover wideband module / auxiliary amplifiers,
        7. ``*RST`` — 0 V, 0 Hz, Standby, all modifiers off,
        8. ``STBY`` — explicit belt and braces,
        9. verify ISR bit 11 (``REMOTE``),
        10. ``*SRE 8`` when *srq* was requested — fault -> SRQ (guide §8.7).

        ``*RST`` and ``STBY`` are **REM-gated**: with REN unasserted they are
        ignored *and* log fault 2213, so ``STBY`` cannot rescue a missing-REN
        condition. Step 9 is what actually catches it — on either interface,
        since a serial session that never reached Remote fails it the same
        way.
        """
        self._t.set_timeout(self.timeout)
        self._t.clear()
        if self.is_serial:
            self._open_serial_port()
        self.cmd("*CLS")
        self.startup_faults = self.drain_faults()

        # Check the manufacturer *before* parsing the field layout: another
        # instrument answering at this address (an HP 3458A returns five
        # fields) must produce "wrong instrument", not "malformed response".
        idn = self.query("*IDN?")
        if not _unquote(idn).upper().startswith("FLUKE"):
            raise Fluke5700AProtocolError(
                f"instrument at this address is not a Fluke calibrator: "
                f"{idn!r}", idn)
        self.identity = parse_idn(idn)

        # The no-options case is undefined (gap G23) and may well come back as
        # a blank line, so an empty *OPT?* response is accepted as "none".
        # A transport failure is *not* swallowed: it would leave a late
        # response in the output buffer, which the ISR? read below would then
        # consume and report as a bogus protocol error.
        self.options = parse_opt(self._query_raw("*OPT?"))

        self.cmd("*RST")
        self.cmd("STBY")

        isr = self.query_int("ISR?")
        if not isr & ISR_REMOTE:
            how = (
                "The REMOTE command did not take. Check that the rear-panel "
                "serial port is selected as the remote-control interface in "
                "the RS-232C Port Setup menu — a calibrator being controlled "
                "from GPIB ignores it — and that the front panel is not in "
                "local lockout."
                if self.is_serial else
                "Assert REN on the bus (most GPIB libraries do so on the "
                "first addressed write) and check the front panel is not in "
                "local lockout.")
            raise Fluke5700AError(
                f"calibrator is not in Remote (ISR=0x{isr:04X}, bit 11 clear). "
                f"*RST and STBY were ignored and fault 2213 was logged. {how}")

        if not isr & ISR_OPER:
            # Steps 7-8 took: the output is off, and this session starts from
            # a *known* zero rather than the "assume full scale" default that
            # standby() sizes its deadline from. Evidence, not assumption --
            # it is read off the ISR? word step 9 already needed.
            self._live_volts = 0.0

        if self.srq:
            self.cmd("*SRE 8")

    def _open_serial_port(self) -> None:
        """Enter Remote and put the port in a machine-readable state.

        Three things must happen before the session's first *parsed* response,
        and all three are serial-only:

        1. ``REMOTE``. There is no REN line here, so this command is what puts
           the calibrator in the remote state; without it every state-changing
           command is silently ignored and logs fault 2213 (guide §7.2).
           ``REMOTE`` over *GPIB* is fault 2211, which is why this is gated on
           the transport rather than sent always.
        2. ``SP_SET COMP``. The port's factory-default response style is
           ``TERM``, whose replies are prose for a human ([OM §5-13, p.5-10])
           — ``*IDN?``, ``FAULT?`` and everything after them would fail to
           parse. This is sent **unconditionally rather than only when
           ``SP_SET?`` reports TERM**, because reading the setting first means
           parsing exactly the prose being escaped from. It is idempotent when
           the port is already COMP, and it changes only the response style,
           never the link parameters, so it cannot cut the connection under
           itself. It does write nonvolatile memory — port configuration, not
           calibration memory.
        3. ``SP_SET?``, parsed and kept on :attr:`serial_setup`, to verify
           step 2 actually took and to reject a ``CR``-only EOL, which would
           make every read wait out the full timeout.

        A calibrator that never reached Remote fails at step 3 rather than
        step 1 — ``SP_SET`` is itself REM-gated, so its refusal shows up as a
        port still in TERM. The error says so.
        """
        self._t.remote()
        self.cmd("SP_SET COMP")
        setup = parse_sp_set(self.query("SP_SET?"))
        if not setup.parseable:
            raise Fluke5700AProtocolError(
                f"the serial port is still in {setup.style} response style "
                f"after SP_SET COMP, so no response can be parsed. Either the "
                f"calibrator never entered Remote (SP_SET is REM-gated: check "
                f"that the serial port is the selected remote-control "
                f"interface) or set Remote I/F to COMPUTER in the RS-232C "
                f"Port Setup menu.", setup.raw)
        if not setup.readable_lines:
            raise Fluke5700AError(
                f"the serial port's EOL is {setup.eol}, and a CR-only "
                f"terminator makes every response line arrive one full "
                f"timeout late. Set EOL to CRLF or LF — from the front panel, "
                f"or with 'SP_SET CRLF' over GPIB.")
        self.serial_setup = setup

    def close(self) -> None:
        """Shut the session down safely per guide §14.4.

        ``STBY``, drain the fault queue (recorded on
        :attr:`shutdown_faults`, never raised), ``*CLS``, close the transport.
        Every step is wrapped because this also runs on the error path: a
        failure in one must not stop the others — above all it must not stop
        the transport from being closed.
        """
        try:
            self.cmd("STBY")
        except Exception:
            pass
        try:
            self.shutdown_faults = self.drain_faults()
        except Exception:
            pass
        try:
            self.cmd("*CLS")
        except Exception:
            pass
        try:
            # GTL, so the operator does not find the front panel still in
            # remote after every session. Optional per transport, and
            # best-effort like everything else on this path.
            self.local()
        except Exception:
            pass
        try:
            self._t.close()
        except Exception:
            pass

    # ── output control (guide §7.3, §11, §14.2) ───────────────────────

    def set_output(self, amplitude: float, unit: str,
                   frequency_hz: float = 0.0, *,
                   keep_range_lock: bool = False) -> None:
        """Source *amplitude* *unit* at *frequency_hz*, per guide §14.2.

        Emits, in this order::

            STBY                                  # safe function transition
            RANGELCK OFF                          # only if the range is locked
            OUT <amp:.10E> OHM                    # ohms: never a frequency
               or
            OUT <amp:.10E> <UNIT>, <freq:.6E> HZ  # everything else, both
            FAULT? ...                            # drain; raise on any

        This deliberately does **not** call ``OPER``. Sourcing and energizing
        are separate steps so a caller can stage a value while still in
        standby; :meth:`operate` is its own method.

        Four of the module's five rules live here:

        * amplitude and frequency always travel together in **one** ``OUT``
          (§7.3) — for dc, ``0 HZ`` is sent explicitly, because a residual
          frequency from a previous setpoint is what turns ``OUT 100V`` into
          "100 V at 1 MHz" and faults;
        * range lock is cleared **before** the ``OUT``, never after (§7.3);
        * no frequency is ever sent in the ohms function — fault 812 (§14.2);
        * the number goes out as a plain exponential with a base unit; no
          multiplier prefix is ever synthesized (§3.6).

        The envelope is validated **offline first** (:func:`validate_output`),
        so a bad test plan raises :class:`ValueError` listing every violation
        without the instrument being touched at all. Only then does any I/O
        happen. Faults left by the ``OUT`` itself (the 8xx envelope family)
        raise :class:`CalibratorFault`.

        *keep_range_lock* is the one documented exception to the second rule,
        and it exists because Fluke's own verification needs it: "Set the
        Calibrator output for 10V, then activate range lock for the 11V range.
        Now set the Calibrator to 0V OPERATE" ([SM 2007] §3-16), and Table 3-19
        note 3, "Use Range Lock to obtain 1V on 11V and 10V on 22V range".
        There is no command that *names* a range, so a below-full-scale point
        on a chosen range can only be reached by anchoring the range with one
        ``OUT``, locking it, and sending the setpoint with the lock still on.
        **The caller owns the coupling rule** (guide §7.3, [OM §5-17]): the new
        amplitude must be inside the locked range or the calibrator faults —
        which is exactly what ``OUT 10V`` after ``OUT 1V ; RANGELCK ON`` does.
        :class:`~caldmm.cal.adapters.Fluke5700ASource` guarantees it from the
        plan's own range and verifies it with ``RANGE?`` before ``OPER``.
        """
        unit_u = str(unit).strip().upper()
        problems = validate_output(amplitude, unit_u, frequency_hz,
                                   options=self.options,
                                   wideband=self._wideband)
        if problems:
            raise ValueError(
                f"refusing to source {amplitude!r} {unit_u} at "
                f"{frequency_hz!r} Hz"
                + (" (wideband active)" if self._wideband else "")
                + ": " + "; ".join(problems))

        self.cmd("STBY")                       # safe transition, guide §14.2
        if not keep_range_lock and self.is_range_locked():
            # Before the OUT, never after -- guide §7.3 worked case 2.
            self.cmd("RANGELCK OFF")
            self._range_locked = False
        if unit_u == "OHM":
            self.cmd(f"OUT {amplitude:.10E} OHM")
        else:
            self.cmd(f"OUT {amplitude:.10E} {unit_u}, {frequency_hz:.6E} HZ")

        faults = self.drain_faults()
        if faults:
            raise CalibratorFault(faults)

        # What is now staged, for standby()'s deadline. Recorded *after* the
        # OUT is known to have been accepted, and deliberately not folded into
        # ``_live_volts``: staging is not energizing, and the engine's own
        # teardown stages 0 V while a kilovolt is still coming down (M52,
        # ``adapters.zero_output``). Only :meth:`operate` promotes it.
        self._staged_output = (amplitude, unit_u)
        self._staged_hz = float(frequency_hz or 0.0)

        # A setpoint change is conservatively treated as both a range and a
        # polarity change; the estimate only ever feeds a timeout.
        self._settle_estimate = expected_settle_seconds(
            unit_u, frequency_hz, range_change=True, polarity_change=True,
            range_id=expected_range_id(amplitude, unit_u, frequency_hz,
                                       options=self.options,
                                       wideband=self._wideband))

    def operate(self, *, settle: bool = True,
                timeout: Optional[float] = None) -> None:
        """Energize the output, then **verify** it actually came on.

        1. :meth:`drain_faults`; if anything came back, raise
           :class:`CalibratorFault` **without** sending ``OPER``. Draining
           alone re-enables the §8.6 inhibit, but a pending fault means the
           requested state is suspect and must not be energized blind.
        2. ``OPER``.
        3. if *settle*, :meth:`wait_settled`.
        4. read ``ISR?`` and verify bit 0 (``OPER``) is set.

        Step 4 is not optional. ``OPER`` is **silently** inhibited for outputs
        of 22 V and over while the fault queue is non-empty (§8.6), and
        several conditions trip the output back to standby by themselves
        (§11.7). A driver that skips the post-check happily records a meter
        reading zero volts as a 100 % error.

        On any failure after ``OPER`` — a settle timeout or a clear ``OPER``
        bit — the output is dropped to standby before the exception
        propagates. A failure here means the output state is unknown, and an
        unknown state at 1100 V is not one to leave energized.

        This is also where the staged setpoint becomes the *live* one for
        :meth:`standby`'s deadline (:meth:`_note_energized`), recorded before
        ``OPER`` goes out rather than after it succeeds: a command written to
        the bus has to be assumed to have arrived.
        """
        faults = self.drain_faults()
        if faults:
            # Do not proceed: OPER is not written at all in this path.
            raise CalibratorFault(faults)

        self._note_energized()
        self.cmd("OPER")
        try:
            if settle:
                self.wait_settled(timeout)
            isr = self.query_int("ISR?")
        except Exception:
            self._safe_standby()
            raise

        if not isr & ISR_OPER:
            self._safe_standby()
            trip_faults = self.drain_faults()
            rendered = ("; ".join(f"{code}: {text}"
                                  for code, text in trip_faults)
                        or "none")
            raise Fluke5700AError(
                f"output did not come on: ISR=0x{isr:04X}, bit 0 (OPER) is "
                f"clear after OPER. Either OPER was inhibited (guide §8.6 — "
                f"outputs >= {HIGH_VOLTAGE_V:g} V are blocked while the "
                f"fault queue is "
                f"non-empty) or the output tripped to standby (guide §11.7 — "
                f"external sense selected with no signal at the SENSE "
                f"terminals, an overload or compliance-limit condition, or "
                f"phase lock enabled but unable to lock). Faults drained "
                f"after the trip: {rendered}")

    def standby(self) -> None:
        """De-energize the output (``STBY``), then **verify** it took effect.

        ``STBY`` is "Overlapped command, ignored if not in remote"
        ([OM Table 5-2, p.190]), and "if sent to the calibrator in the local
        state, the command will not work, and logs a fault into the fault
        queue" ([OM §5-19, p.162] — fault 2213). The operator's "Go To Local
        Control" softkey ([OM Table 5-5, p.188]) is reachable mid-run, so a
        ``STBY`` that the instrument ignored is not a theoretical state: it is
        one keypress away, and this is the primitive every caller uses as
        *the* de-energize action. Writing the command and returning would
        report success while up to 1100 V stayed live at the UUT.

        So, after the write: poll ``ISR?`` for at most
        :func:`standby_verify_timeout` of the output that was last energized
        (the command is overlapped, so the first read can still show the old
        state) and require

        * bit 0 (``OPER``) **clear** — the output really came off, and
        * bit 11 (``REMOTE``) **set** — the calibrator was in a state where
          ``STBY`` is obeyed at all.

        Either failing raises :class:`Fluke5700AError`. It still does **not**
        drain the fault queue — draining is what makes it unsafe in a
        ``finally``, and the queue's own evidence (fault 2213) is named in the
        message instead. Transport failures still propagate.

        **The deadline is sized, the check is not weakened.** A 15-point dc
        run that ended on -1000 V was reported as "the output may still be
        live" with every point measured and ``ISR?`` reading OPER-clear
        moments later: 1.0 s is right for 100 mV and wrong for a kilovolt.
        So the window now scales with the magnitude that was energized —
        floor unchanged at :data:`STANDBY_VERIFY_TIMEOUT_SEC` below
        :data:`HIGH_VOLTAGE_V`, up to :data:`STANDBY_VERIFY_TIMEOUT_MAX_SEC`
        at full scale — while a stuck output still raises, and the REMOTE-bit
        check is untouched. Sizing costs nothing when the output does come
        off, because the poll exits on the bit, not on the clock.

        This method can now raise where it once could not. That is deliberate:
        a caller in a ``finally`` that loses its original exception to a
        "the output may still be live" error has been told the more important
        of the two. :meth:`_safe_standby` is the variant for a path that is
        already handling a failure.
        """
        limit = max(STANDBY_RETRY_MIN_SEC,
                    standby_verify_timeout(self._live_volts, self._live_amps,
                                           low_frequency=self._live_low_freq))
        sized_for = ("an unknown magnitude" if self._live_volts is None
                     else f"{self._live_amps:g} A" if self._live_amps
                     else f"{self._live_volts:g} V")
        if self._live_low_freq and self._live_volts is not None:
            sized_for += " ac below 100 Hz"

        self.cmd("STBY")
        deadline = time.monotonic() + limit
        while True:
            isr = self.query_int("ISR?")
            if not isr & ISR_OPER:
                break
            if time.monotonic() >= deadline:
                raise Fluke5700AError(
                    f"STBY did not de-energize the output: ISR=0x{isr:04X}, "
                    f"bit 0 (OPER) is still set {limit:.1f} s after STBY — "
                    f"the deadline for an output of {sized_for}. The output "
                    f"must be assumed live — press the front-panel STANDBY "
                    f"key. If bit 11 (REMOTE) is clear the calibrator has "
                    f"left the remote state and ignored the command "
                    f"(fault 2213, [OM Table 5-2, p.190])")
            time.sleep(STANDBY_POLL_INTERVAL_SEC)
        # Proven off. The next standby() starts from "nothing is live" and
        # gets the floor again, until something is energized.
        self._live_volts = 0.0
        self._live_amps = 0.0
        self._live_low_freq = False
        if not isr & ISR_REMOTE:
            raise Fluke5700AError(
                f"STBY was sent to a calibrator that is not in remote: "
                f"ISR=0x{isr:04X}, bit 11 (REMOTE) is clear. The command was "
                f"ignored and logged fault 2213 ([OM Table 5-2, p.190]; "
                f"[OM §5-19, p.162]). The output happens to be off (bit 0 "
                f"OPER is clear), but this session cannot command it — return "
                f"the calibrator to remote before continuing")

    def _note_energized(self) -> None:
        """Promote the staged setpoint to "live", for :meth:`standby`.

        The **largest** magnitude energized since the last verified standby
        wins, and staging a smaller value does not lower it. That is not
        pedantry: the engine's teardown programs 0 V *while the previous
        point is still coming down* (M52, ``adapters.zero_output`` — "Press
        RESET, or 0 V ENTER") and only then calls :meth:`standby`, so a
        driver that took its deadline from the last ``OUT`` would size the
        -1000 V teardown as if it were a 0 V one, which is the bug this
        exists to prevent. A staged setpoint the driver never saw leaves the
        magnitude unknown, and :func:`standby_verify_timeout` reads that as
        the worst case.
        """
        if self._live_volts is None:
            return
        staged = self._staged_output
        if staged is None:
            self._live_volts = None
            return
        amplitude, unit = staged
        self._live_volts = max(self._live_volts,
                               abs(amplitude) if unit == "V" else 0.0)
        self._live_amps = max(self._live_amps,
                              abs(amplitude) if unit == "A" else 0.0)
        if 0.0 < self._staged_hz < STANDBY_LOW_FREQUENCY_HZ:
            self._live_low_freq = True

    def _forget_output(self) -> None:
        """The output moved out from under the driver; assume the worst.

        Called by every command that changes the magnitude without naming it
        (``INCR``, ``MULT``, ``NEWREF``). :func:`standby_verify_timeout`
        reads ``None`` as "use the ceiling", which is the safe direction: an
        over-long deadline is only ever spent on a de-energize that failed.
        """
        self._staged_output = None
        self._live_volts = None

    def _safe_standby(self) -> None:
        """``STBY`` for a path that is already handling a failure.

        Never raises — it runs inside ``except`` blocks — but never *silently*
        fails either: a failed de-energize is recorded on
        :attr:`standby_error` and printed to stderr, because "the output may
        still be live" outranks whatever error brought us here.
        """
        try:
            self.standby()
        except Exception as exc:                 # noqa: BLE001 - safety path
            self.standby_error = exc
            print(f"5700A: STANDBY VERIFICATION FAILED - the output may "
                  f"still be live: {exc}", file=sys.stderr)
        else:
            self.standby_error = None

    def wait_settled(self, timeout: Optional[float] = None, *,
                     poll_interval: float = 0.1) -> None:
        """Block until ISR bit 12 (``SETTLED``) is set.

        Deliberately **not** a blocking ``*OPC?`` read. ``*OPC?`` holds the
        GPIB controller for up to 45 s, which starves a co-resident HP 3458A
        on the same bus — a free-running 3458A's output buffer keeps filling
        while nobody can read it. ``hp3458a.read_single_sample()`` establishes
        the bus-friendly idiom used here: release the bus, sleep, poll.

        The mandatory :data:`SETTLE_STATUS_LATENCY_SEC` guard is slept
        **first**. Guide §8.4: "the 5700A/5720A Series II can take up to 60 ms
        to set the SETTLING bit". Polling before that has elapsed can read a
        stale ``SETTLED = 1`` left from the *previous* setpoint and return
        instantly, which is precisely the silent-wrong-data failure this
        driver is built to prevent.

        *timeout* defaults to :data:`SETTLE_TIMEOUT_MARGIN` times the
        :func:`expected_settle_seconds` estimate for the last
        :meth:`set_output`, floored at :data:`DEFAULT_TIMEOUT_SEC` (45 s) per
        guide §11.5's recommendation. Raises a bare :class:`TimeoutError` on
        expiry, matching ``hp3458a.py``.
        """
        if timeout is None:
            timeout = self.default_settle_timeout()
        deadline = time.monotonic() + timeout
        time.sleep(SETTLE_STATUS_LATENCY_SEC)
        while True:
            isr = self.query_int("ISR?")
            if isr & ISR_SETTLED:
                return
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"5700A: output not settled after {timeout:.0f} s "
                    f"(ISR=0x{isr:04X}, bit 12 SETTLED clear)")
            time.sleep(poll_interval)

    def default_settle_timeout(self) -> float:
        """The timeout :meth:`wait_settled` uses when not given one."""
        estimate = self._settle_estimate or SETTLE_WORST_CASE_SEC
        return max(DEFAULT_TIMEOUT_SEC, SETTLE_TIMEOUT_MARGIN * estimate)

    # ── readback (guide §9, §11.4) ────────────────────────────────────

    def out_actual(self) -> "tuple[float, str, float]":
        """``OUT?`` -> ``(amplitude, unit, frequency_hz)``.

        .. warning::
           **In the resistance function this returns the *characterized*
           value the calibrator is actually sourcing, not the nominal value
           that was requested** (guide §11.4 item 2; [OM §5-62, p.5-70]:
           "if you request 100 Ω, the 5700A/5720A Series II sources a
           characterized value that is close to 100 Ω"). Every error
           computation must use **this** value as the reference. Using the
           requested nominal instead silently folds the calibrator's own
           ohms characterization into the UUT's error — for the whole
           calibration subsystem, that is the difference between a valid
           result and a fabricated one.

        Amplitudes never carry multipliers (§11.4 item 3): always base units.
        """
        return parse_out(self.query("OUT?"))

    def adj_actual(self) -> "tuple[float, str, float]":
        """``ADJOUT?`` -> ``(amplitude, unit, frequency_hz)``.

        Identical to :meth:`out_actual` in every function **except**
        resistance, where ``OUT?`` gives the characterized value actually
        sourced and ``ADJOUT?`` gives what the Control Display would read
        (guide §9 ``ADJOUT?``). Elsewhere it reports the output as modified by
        the front-panel knob or ``INCR``.
        """
        return parse_out(self.query("ADJOUT?"))

    def vout_actual(self) -> "tuple[float, str, float]":
        """``VOUT?`` -> ``(amplitude, unit, frequency_hz)``, dBm as volts.

        Identical to :meth:`out_actual` except that a dBm output is expressed
        in volts ([OM p.5-41]). The dBm reference changes with the function —
        600 Ω on the standard ac output, 50 Ω on wideband — so 42 dBm can be
        ~97 V on the main terminals. Never convert dBm locally; this query is
        the instrument's own conversion and the only trustworthy one.
        """
        return parse_out(self.query("VOUT?"))

    def dbmout_actual(self) -> "tuple[float, str, float]":
        """``DBMOUT?`` -> ``(amplitude, unit, frequency_hz)``.

        Same as :meth:`out_actual` except an ac-voltage output is converted
        to dBm for the appropriate load — 50 Ω for wideband, 600 Ω for
        standard ac V ([OM p.5-26]). Non-voltage outputs come back in their
        native unit (``A``, ``OHM``).
        """
        return parse_out(self.query("DBMOUT?"))

    def uncert(self) -> "tuple[Optional[float], str, int]":
        """``UNCERT?`` -> ``(uncertainty, units, interval_days)``.

        The uncertainty is ``None`` when the calibrator has no specification
        for the present output (the ``-1.0`` sentinel, guide §9). The interval
        is set by ``CAL_INTV`` and the confidence level by ``CAL_CONF``.
        """
        return parse_uncert(self.query("UNCERT?"))

    def range_id(self) -> str:
        """``RANGE?`` -> the present §10 range identifier, e.g. ``DC220V``.

        The authority on which range is selected. :func:`expected_range_id`
        only *predicts* it from the manual's tables, two of whose identifiers
        are misprinted (guide §10).
        """
        return _unquote(self.query("RANGE?")).upper()

    # ── modifier state (guide §9) ─────────────────────────────────────

    def is_range_locked(self) -> bool:
        """True when ISR bit 5 (``RLOCK``) is set.

        Read from the instrument rather than from driver state: the front
        panel can lock the range behind the driver's back, and an ``ISR?`` is
        one cheap query against the cost of the §7.3 coupling fault.
        """
        self._range_locked = bool(self.query_int("ISR?") & ISR_RLOCK)
        return self._range_locked

    def range_lock(self, on: bool) -> None:
        """``RANGELCK ON|OFF`` — lock or unlock the present output range.

        The range unlocks by itself when the output function changes
        (``RANGELCK``, [OM p.5-36]). :meth:`set_output` clears the lock before
        every ``OUT`` **unless called with** ``keep_range_lock=True``, the one
        documented exception (§7.3 worked case 2; see that method's
        docstring) that lets a caller source a second setpoint on the range it
        just anchored and locked. Otherwise, if a locked range is wanted, lock
        it again afterwards.
        """
        self.cmd(f"RANGELCK {'ON' if on else 'OFF'}")
        self._range_locked = bool(on)

    def rcomp(self, on: bool) -> None:
        """``RCOMP ON|OFF`` — two-wire ohms compensation.

        Valid only while a resistance output of :data:`RCOMP_MAX_OHMS`
        (19 kΩ) or lower is selected (guide §11.2; fault 836 otherwise), so
        enabling it reads back ``OUT?`` first and raises :class:`ValueError`
        if the present output does not qualify. Switching it **off** is always
        allowed — it is a cleanup path.

        The readback is snapped to its nominal catalogue value before being
        compared (:func:`snap_resistance`). ``OUT?`` in ohms returns the
        *characterized* resistance, which sits above the nominal about half
        the time — comparing 19000.42 Ω against a nominal 19 kΩ limit would
        reject a perfectly legal 19 kΩ setpoint at random.
        """
        if on:
            amplitude, unit, _ = self.out_actual()
            if unit != "OHM":
                raise ValueError(
                    f"RCOMP is a resistance-function modifier; the present "
                    f"output is in {unit} (fault 831)")
            nominal = snap_resistance(abs(amplitude))
            if nominal is None:
                raise ValueError(
                    f"the present output, {_fmt(amplitude)} OHM, is not "
                    f"within {RESISTANCE_SNAP_REL_TOL:.0%} of any catalogue "
                    f"resistance; refusing to enable two-wire compensation "
                    f"(fault 831)")
            if not _le(nominal, RCOMP_MAX_OHMS):
                raise ValueError(
                    f"two-wire compensation is available only for "
                    f"{_fmt(RCOMP_MAX_OHMS)} OHM and lower; the present "
                    f"output is {_fmt(amplitude)} OHM (nominal "
                    f"{_fmt(nominal)} OHM) (fault 836)")
        self.cmd(f"RCOMP {'ON' if on else 'OFF'}")

    def ext_sense(self, on: bool) -> None:
        """``EXTSENSE ON|OFF`` — remote (4-wire) voltage sensing.

        Guide §11.7: external sense selected with **no signal at the SENSE
        terminals** trips the output to standby with a limit error, so
        :meth:`operate`'s post-check is what catches a mis-wired fixture.
        """
        self.cmd(f"EXTSENSE {'ON' if on else 'OFF'}")

    @property
    def wideband(self) -> bool:
        """The cached WBAND state — **no I/O**.

        What :meth:`set_output` validates against. :meth:`is_wideband`
        re-reads ISR? when the front panel may have interfered.
        """
        return self._wideband

    def local(self) -> None:
        """Return the front panel to local control (IEEE-488 GTL).

        The serial-only ``LOCAL`` command duplicates GTL and does not exist
        over GPIB ([OM Table 5-3]); the bus message is the way. Raises
        :class:`Fluke5700AError` when the transport cannot send one.
        :meth:`close` sends it best-effort so a finished session does not
        leave the panel remote-locked.
        """
        sender = getattr(self._t, "local", None)
        if sender is None:
            raise Fluke5700AError(
                "this transport cannot send GTL (no local() method)")
        sender()

    def is_wideband(self) -> bool:
        """True when ISR bit 10 (``WBND``) is set.

        Read from the instrument rather than from driver state: the
        front-panel ``W BND`` key can toggle the module behind the driver's
        back, and the cache this refreshes is what :meth:`set_output` uses to
        pick its validation envelope.
        """
        self._wideband = bool(self.query_int("ISR?") & ISR_WBND)
        return self._wideband

    def wband(self, on: bool) -> None:
        """``WBAND ON|OFF`` — Option 5700A-03 wideband module output.

        Faults are drained and raised: ``WBAND ON`` on a unit without the
        module is fault 800 (Wideband Module Needed), and the undocumented
        ``*OPT?`` spelling for the module means there is no reliable offline
        gate — the instrument is the authority.

        Switching **off** ([OM §4-30]): if the present output is an ac
        voltage the standard function can source, that value is selected;
        otherwise the output becomes 0 mV dc. Either way the settle estimate
        from the last :meth:`set_output` no longer applies, so it is cleared
        and the next :meth:`wait_settled` falls back to the 45 s floor. The
        staged setpoint goes with it, for the same reason: the calibrator,
        not this driver, chose what the output became.
        """
        self.cmd(f"WBAND {'ON' if on else 'OFF'}")
        faults = self.drain_faults()
        if faults:
            raise CalibratorFault(faults)
        self._wideband = bool(on)
        self._settle_estimate = None
        self._forget_output()

    def cur_post(self) -> str:
        """``CUR_POST?`` -> which binding posts carry the current output.

        One of :data:`CUR_POSTS`. Worth reading before every current group:
        the setting is **nonvolatile** and untouched by ``*RST`` ([OM Table
        5-2, p.176]: "Once set, the calibrator retains the current post
        setting during power-off periods"), so nothing on the bus otherwise
        says whether the current will leave by OUTPUT HI or by AUX CURRENT
        OUTPUT — and no meter can tell the difference between the wrong post
        and a broken lead.
        """
        return _unquote(self.query("CUR_POST?")).upper()

    def set_cur_post(self, post: str) -> None:
        """``CUR_POST NORMAL|AUX|IB5725`` — select the current output posts.

        ``NORMAL`` is the OUTPUT HI binding post (the factory default),
        ``AUX`` the AUX CURRENT OUTPUT post, ``IB5725`` the 5725A's
        ([OM Table 5-2, p.176]). Sourcing current into a post the UUT is not
        wired to is an open circuit: the calibrator hits its compliance limit
        and trips (fault 1819), which looks like a dead output rather than a
        wiring mistake.

        A rear-output calibrator cannot use ``AUX`` — [OM §4-27, p.114]: "If
        AUX is selected for a calibrator configured for rear output, error
        message: 'Compliance voltage exceeded' will be displayed" — but that
        shows up at the *output*, not here, so this only checks the token.

        "Sequential command, ignored if not in remote", so the fault queue is
        drained afterwards and anything in it raises
        :class:`CalibratorFault`: an ignored ``CUR_POST`` that returned
        quietly would leave the post it was called to make deterministic in
        whatever state the last operator left it.
        """
        self._cmd_checked(f"CUR_POST {_one_of(post, CUR_POSTS, 'CUR_POST')}")

    def ext_guard(self, on: bool) -> None:
        """``EXTGUARD ON|OFF`` — external voltage guard.

        Related faults 853 (Cannot Use External Guard Now) and 854 (Cannot Use
        Ext Guard On Selected Range); ISR bit 1 is ``EXGARD``.
        """
        self.cmd(f"EXTGUARD {'ON' if on else 'OFF'}")

    # ── output editing and entry limits (guide §9, §14.5) ─────────────

    def incr(self, step: float, unit: Optional[str] = None) -> None:
        """``INCR`` — nudge the output and **enter error mode** (§14.5).

        The remote twin of the front-panel adjustment knob, and the step the
        documented ``CAL_RNG`` / ``CAL_WBFLAT`` nulling workflows are built
        on. *step* is signed; *unit* is ``None`` for the present function's
        magnitude or ``"HZ"`` for frequency (fault 823 guards a frequency
        adjusted to <= 0). Any other unit is refused offline: the guide's own
        examples use exactly these two forms, and a synthesized unit string
        is how multiplier-prefix bugs (§3.6) happen.

        Faults are drained and raised, so a rejected step (an edit the
        present output cannot take) is an exception, not a silence. Note the
        output reference is unchanged — ``OUT?`` reports the reference,
        ``ADJOUT?`` the adjusted value — until :meth:`newref` locks the edit
        in.
        """
        step = float(step)
        if not math.isfinite(step) or step == 0.0:
            raise ValueError(
                f"INCR step must be a finite non-zero number, got {step!r}")
        unit_u = str(unit).strip().upper() if unit else ""
        if unit_u not in ("", "HZ"):
            raise ValueError(
                f"INCR unit must be omitted (magnitude) or 'HZ' (frequency), "
                f"got {unit!r}")
        self.cmd(f"INCR {step:.10E}" + (" HZ" if unit_u else ""))
        self._forget_output()
        faults = self.drain_faults()
        if faults:
            raise CalibratorFault(faults)

    def newref(self) -> None:
        """``NEWREF`` — take the present (adjusted) output as the reference.

        Same as pressing ``NEW REF`` locally: leaves error mode with the
        edited value as the new reference point.
        """
        self.cmd("NEWREF")
        self._forget_output()
        faults = self.drain_faults()
        if faults:
            raise CalibratorFault(faults)

    def mult(self, factor: float) -> None:
        """``MULT`` — multiply the reference magnitude by *factor*.

        With the output at 10 V (and error mode off), ``mult(1.9)`` moves it
        to 19 V. The result must still fit the envelope — the instrument
        faults if it does not, and the drain raises it here.
        """
        factor = float(factor)
        if not math.isfinite(factor) or factor == 0.0:
            raise ValueError(
                f"MULT factor must be a finite non-zero number, got "
                f"{factor!r}")
        self.cmd(f"MULT {factor:.10E}")
        self._forget_output()
        faults = self.drain_faults()
        if faults:
            raise CalibratorFault(faults)

    def set_limits(self, positive: float, negative: float,
                   unit: str = "V") -> None:
        """``LIMIT`` — bound the output magnitudes the calibrator will take.

        The one hardware interlock the instrument itself offers an operator:
        with ``set_limits(25.0, -25.0)`` a later request beyond ±25 V is
        refused by the calibrator (faults 815/856), whatever software asked
        for it. The voltage limit bounds dc and ac volts; the current limit
        (``unit="A"``) bounds dc and ac amps ([OM §4-51]). Power-up and
        ``*RST`` reset the limits to the full envelope.

        *positive* must be > 0 and *negative* <= 0 (fault 814 is the
        instrument's word for getting that backwards; this raises before any
        I/O instead). Both parameters are sent in one command, as required.
        """
        unit_u = str(unit).strip().upper()
        if unit_u not in ("V", "A"):
            raise ValueError(f"LIMIT unit must be V or A, got {unit!r}")
        positive, negative = float(positive), float(negative)
        if not (math.isfinite(positive) and math.isfinite(negative)):
            raise ValueError("LIMIT values must be finite numbers")
        if positive <= 0 or negative > 0:
            raise ValueError(
                f"LIMIT takes a positive and a non-positive magnitude, got "
                f"{positive!r} and {negative!r} (fault 814)")
        self.cmd(f"LIMIT {positive:.10E} {unit_u}, {negative:.10E} {unit_u}")
        faults = self.drain_faults()
        if faults:
            raise CalibratorFault(faults)

    def limits(self) -> "tuple[float, float, float, float]":
        """``LIMIT?`` -> ``(+V limit, -V limit, +A limit, -A limit)``.

        Parsed numerically; the OM's printed example for this query is
        internally inconsistent (guide §13) and is not relied on.
        """
        fields = _fields(self.query("LIMIT?"), 4, "LIMIT?")
        resp = ",".join(fields)
        return (_to_float(fields[0], resp, "LIMIT? +V"),
                _to_float(fields[1], resp, "LIMIT? -V"),
                _to_float(fields[2], resp, "LIMIT? +A"),
                _to_float(fields[3], resp, "LIMIT? -A"))

    # ── latched status-change reads (guide §8.4) ──────────────────────

    def iscr(self) -> int:
        """``ISCR?`` — read **and clear** the Instrument Status Change
        Register.

        The latched view: a bit is set here if the corresponding ISR bit
        *changed* since the last ``ISCR?``, so a transient — an output that
        tripped and recovered between polls — is caught where a plain
        ``ISR?`` poll would miss it. Decode with :func:`decode_isr` (same bit
        map).
        """
        return self.query_int("ISCR?")

    def isce(self, mask: int) -> None:
        """``ISCE`` — set the Instrument Status Change Enable mask (§8.4)."""
        mask = int(mask)
        if not 0 <= mask <= 0xFFFF:
            raise ValueError(f"ISCE mask must be 0..65535, got {mask!r}")
        self.cmd(f"ISCE {mask}")

    def isce_query(self) -> int:
        """``ISCE?`` — read back the Instrument Status Change Enable mask."""
        return self.query_int("ISCE?")

    # ── helpers shared by the calibration / diagnostic surface ────────

    @contextlib.contextmanager
    def _temporary_timeout(self, seconds: float):
        """Run the body with a different transport timeout, then restore it.

        The session timeout is restored on **every** exit path. Leaving a
        shortened timeout behind is the exact failure the block reader's
        ``restore_timeout`` exists to prevent: the next slow operation — an
        ``*ESR?`` poll during ``CAL_ADJ``, say — then fails spuriously.
        """
        self._t.set_timeout(seconds)
        try:
            yield
        finally:
            self._t.set_timeout(self.timeout)

    def _require_service(self, command: str) -> None:
        if not self.service:
            raise Fluke5700AError(
                f"{command} is a service-only command (guide §16; the "
                f"calibrator answers fault 2217 'Command Reserved For "
                f"Service'). Construct the driver with service=True if you "
                f"really mean to use it — it is not part of the documented "
                f"operator command set.")

    def _raise_for_faults(self, faults: "list[tuple[int, str]]", *,
                          what: str) -> None:
        """Raise for a drained fault list, specializing the switch lockout."""
        if not faults:
            return
        if any(code in CALIBRATION_SWITCH_FAULT_CODES for code, _ in faults):
            raise CalibrationSwitchError(faults, what=what)
        raise CalibratorFault(faults)

    def _cmd_checked(self, cmd_str: str) -> None:
        """Write *cmd_str*, drain the fault queue and raise on anything in it.

        The default path for the configuration commands (``DIAGFLT``,
        ``CAL_TEMP``, ``CAL_INTV`` …): they answer nothing, so the fault queue
        is the only evidence that a keyword was rejected.
        """
        self.cmd(cmd_str)
        self._raise_for_faults(self.drain_faults(), what=cmd_str)

    # ── long-term commands (guide §7.4, §7.5) ─────────────────────────

    def _run_long_term(self, cmd: str, *,
                       progress_cb: "Optional[Callable[[int, str], None]]"
                       = None,
                       timeout: float = LONG_TERM_TIMEOUT_SEC,
                       poll_interval: float = LONG_TERM_POLL_INTERVAL_SEC,
                       stop_states: Sequence[int] = (),
                       raise_on_error_bits: bool = True
                       ) -> "tuple[int, str]":
        """Run one long-term command and wait for it, per guide §7.5.

        Returns the last ``(code, substate)`` read from ``STATE?``, so a
        caller can tell *how* the wait ended — completed, or stopped on a
        *stop_states* entry — without spending another query on a second
        ``STATE?``.

        Deliberately **not** a blocking ``*OPC?`` read. ``CAL_ADJ`` and
        ``DIAG ALL`` run for minutes, and a blocking read holds the GPIB
        controller for the whole time, starving every other instrument on the
        bus. The manual's own alternative is used instead: send
        ``<cmd>;*OPC``, then poll ``*ESR?`` for bit 0 while polling ``STATE?``
        for progress (``STATE?`` is safe during a long-term command and is the
        intended progress mechanism — guide §7.4, §9 ``STATE?``).

        **The ``*ESR?`` byte is accumulated across polls.** Reading the ESR
        clears it (guide §8.3), so a poller that tests bit 0 and throws the
        rest away silently discards any CME / EXE / DDE raised *during* the
        operation — gap §19.8 item 3. Each read is OR-ed into a running mask
        and the mask is evaluated at the end; with
        *raise_on_error_bits* those bits produce a :class:`CalibratorFault`
        built from the drained queue.

        The ESR is **read once and discarded immediately before the write**.
        Guide §8.3: an ESR bit stays set until the controller reads the
        register, a device clear happens or ``*CLS`` is sent — and
        :meth:`drain_faults` empties the fault *queue* without touching the
        ESR. So a CME/EXE/DDE left by any earlier rejected command would
        otherwise survive to be OR-ed into this command's mask and reported
        against an operation that actually succeeded. The pre-read is a
        destructive read, not a ``*CLS``, and it happens **before** the
        ``*OPC`` is armed, so the §7.5 deadlock prohibition does not apply.

        *stop_states* lists ``STATE?`` codes that end the wait even though no
        ``*OPC`` has arrived. :meth:`diag` passes
        :data:`STATE_DIAG_HALTED` with it, because a diagnostic halted by a
        fault under ``DIAGFLT HALT`` is *waiting*, not complete, and would
        otherwise poll until the timeout expired. **They are not honoured on
        the first poll**: resuming from a halt with ``DIAG CONT``, the
        *starting* state is the stop state, and whether the calibrator has
        updated ``STATE?`` by the time a zero-delay first poll lands is
        undocumented. Honouring it there would let :meth:`diag_cont` return
        instantly with the run still halted, and the documented stepping loop
        would spin forever.

        A failed ``STATE?`` poll does **not** abandon the operation: the code
        reported to *progress_cb* becomes :data:`STATE_UNKNOWN` and the wait
        continues, up to :data:`MAX_PROGRESS_POLL_FAILURES` consecutive
        failures. Progress reporting is decoration; the ``*ESR?`` poll is the
        completion signal and stays strict.

        .. warning::
           **There is no remote abort** (guide §19.2, gap G6). ``STBY`` is
           itself a state-changing command and is rejected with fault
           :data:`FAULT_LONG_TERM_IN_PROGRESS` while a long-term command runs,
           so this method never tries one — not even on timeout. A
           :class:`TimeoutError` from here means "it did not finish in time",
           **not** "the calibrator is now idle": the operation is probably
           still running. The front-panel ``STBY`` key or line power is the
           only way to stop it. Keep one within the operator's reach before
           starting any long-term command, and do not start one with an output
           of 22 V or more live.

           ``*CLS`` is never sent while the ``*OPC`` is outstanding either —
           it terminates the pending operation-complete and deadlocks with
           fault 2208 (guide §7.5).

        Durations: ``CAL_ZERO`` is ≈2½ minutes (:data:`CAL_ZERO_TIMEOUT_SEC`).
        For ``CAL_CHK``, ``CAL_ADJ``, ``CAL_REF`` and every ``DIAG`` target
        the manuals give **no** figure at all (gaps G10, G11), so
        :data:`LONG_TERM_TIMEOUT_SEC` is a generous guess — measure the real
        duration on first use and pass it explicitly.
        """
        head = cmd.split()[0].upper() if cmd.split() else ""
        if head not in LONG_TERM_COMMANDS:
            raise ValueError(
                f"{cmd!r} is not a long-term command; this runner arms an "
                f"*OPC and polls for it, which only completes for "
                f"{', '.join(sorted(LONG_TERM_COMMANDS))} (guide §7.4)")

        deadline = time.monotonic() + timeout
        stop = frozenset(stop_states)
        # Clear whatever an earlier rejected command left in the ESR, so its
        # CME/EXE/DDE cannot be misattributed to this one (guide §8.3).
        self._query_raw("*ESR?")
        self.cmd(f"{cmd};*OPC")
        esr_seen = 0
        code, substate = STATE_UNKNOWN, ""
        first_poll = True
        state_failures = 0
        while True:
            esr_seen |= self.query_int("*ESR?")
            try:
                code, substate = parse_state(self.query("STATE?"))
                state_failures = 0
            except Fluke5700AError as exc:
                state_failures += 1
                if state_failures > MAX_PROGRESS_POLL_FAILURES:
                    raise
                code, substate = STATE_UNKNOWN, f"progress poll failed: {exc}"
            if progress_cb is not None:
                progress_cb(code, substate)
            if esr_seen & ESR_OPC:
                break
            # Not on the first poll: see the docstring on DIAG CONT.
            if code in stop and not first_poll:
                break
            first_poll = False
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"5700A: {cmd} did not complete within {timeout:.0f} s "
                    f"(accumulated *ESR=0x{esr_seen:02X}, last STATE?="
                    f"{code} {STATE_NAMES.get(code, 'unknown')!r}: "
                    f"{substate!r}). It is probably still running: there is "
                    f"no remote abort for a long-term command and STBY is "
                    f"rejected with fault "
                    f"{FAULT_LONG_TERM_IN_PROGRESS} while one is in progress "
                    f"(guide §19.2 gap G6). Do NOT assume the calibrator is "
                    f"idle — use the front-panel STBY key or line power if "
                    f"the output has to come off.")
            time.sleep(poll_interval)

        if not raise_on_error_bits:
            return code, substate
        if esr_seen & (ESR_CME | ESR_EXE | ESR_DDE):
            faults = self.drain_faults()
            if faults:
                raise CalibratorFault(faults)
            flags = ", ".join(name for bit, name in _ESR_BITS
                              if esr_seen & (1 << bit)
                              and name in ("CME", "EXE", "DDE"))
            raise Fluke5700AError(
                f"5700A: {cmd} completed with error bits set in the Event "
                f"Status Register ({flags}, accumulated *ESR="
                f"0x{esr_seen:02X}) but the fault queue was empty")
        return code, substate

    # ── diagnostics (guide §9 DIAG/DIAGFLT/*TST?, §16) ────────────────

    def state(self) -> "tuple[int, str]":
        """``STATE?`` -> ``(code, substate)``; see :data:`STATE_NAMES`.

        Safe to poll during a long-term command. Branch on the integer code
        only — the substate strings are never enumerated (gap G21), so log
        them and nothing more. Watch for :data:`STATE_TRIPPED`.
        """
        return parse_state(self.query("STATE?"))

    def diag(self, target: str = "D5700", *,
             progress_cb: "Optional[Callable[[int, str], None]]" = None,
             timeout: float = LONG_TERM_TIMEOUT_SEC,
             poll_interval: float = LONG_TERM_POLL_INTERVAL_SEC
             ) -> "list[tuple[int, str]]":
        """Run a self-diagnostic and return the faults it logged.

        *target* is one of :data:`DIAG_TARGETS`: ``ALL``, ``D5700`` (the
        calibrator alone — the default), ``DV5725`` / ``DI5725`` (the
        amplifier), or the two stepping controls ``CONT`` and ``ABORT``.

        The faults are drained **after** the run and returned rather than
        raised: on a unit with a suspect A14 High Voltage Control PCA the
        3100-level codes are the point of the exercise, not an error. They
        decode offline from :data:`FAULT_CODES` when the instrument's own
        ``EXPLAIN?`` misbehaves.

        With ``DIAGFLT HALT`` selected (see :meth:`diagflt_temporarily`) the
        diagnostic **stops at the first fault and waits**. This method returns
        then, with that fault, because :data:`STATE_DIAG_HALTED` is an exit
        condition for the wait; :meth:`diag_halted` distinguishes "halted,
        waiting" from "finished". :meth:`diag_cont` steps to the next fault
        and :meth:`diag_abort` gives up. That loop is the A14 isolation
        workflow::

            with cal.diagflt_temporarily("HALT"):
                faults = cal.diag("D5700")
                while cal.diag_halted():
                    faults += cal.diag_cont()

        ``ABORT`` is not waited on — there is nothing left to complete.

        No duration is documented for any target (gap G11); see
        :meth:`_run_long_term` on the timeout, and on the fact that there is
        **no remote abort for the wait itself**.
        """
        target_u = _one_of(target, DIAG_TARGETS, "DIAG target")
        if target_u == "ABORT":
            self.cmd("DIAG ABORT")
            self.last_diag_state = None
            return self.drain_faults()
        code, _ = self._run_long_term(
            f"DIAG {target_u}", progress_cb=progress_cb, timeout=timeout,
            poll_interval=poll_interval, stop_states=(STATE_DIAG_HALTED,),
            raise_on_error_bits=False)
        self.last_diag_state = code
        return self.drain_faults()

    def diag_cont(self, **kwargs) -> "list[tuple[int, str]]":
        """``DIAG CONT`` — resume a diagnostic halted by a fault."""
        return self.diag("CONT", **kwargs)

    def diag_abort(self) -> "list[tuple[int, str]]":
        """``DIAG ABORT`` — give up on a halted or running diagnostic.

        The one long-term command that *does* have a documented abort
        (guide §19.2 gap G6 — ``CAL_*`` have none).
        """
        return self.diag("ABORT")

    def diag_halted(self) -> bool:
        """True when the last :meth:`diag` stopped at a fault and is waiting.

        Reported from :attr:`last_diag_state` — the ``STATE?`` value the run
        itself already read — rather than by issuing another ``STATE?``. That
        keeps the stepping loop honest: a fresh query could race the
        instrument's own state update and be answered from a moment other than
        the one the run ended at. :meth:`state` is the live query.
        """
        return self.last_diag_state == STATE_DIAG_HALTED

    def diagflt(self, mode: str) -> None:
        """``DIAGFLT HALT|ABORT|CONT`` — what a diagnostic fault does.

        **It governs ``DIAG`` only, not the ``CAL_*`` activities** — measured,
        not inferred. On 2026-08-15 a ``CAL_CHK`` on one unit stopped at the
        ``dcV 220V range gain`` step on fault 2516 with ``DIAGFLT`` at its
        ``ABORT`` default, and stopped at the *same* step, with the same single
        fault, when the check was re-run under ``CONT`` (STATE returned to 0 at
        417 s and 404 s respectively). So a calibration activity always
        terminates on its first fault, and there is no way to make one run on
        and collect the rest. The manual never says this either way; every
        sentence of the ``DIAGFLT`` entry says "diagnostics" and ``CAL_CHK``'s
        entry is silent on faults.

        .. warning::
           **This setting lives in nonvolatile memory** and its factory value
           is :data:`DIAGFLT_DEFAULT` (``ABORT``). Setting ``HALT`` changes
           front-panel behaviour permanently, not just for this session — the
           next person to run diagnostics from the front panel will find them
           stopping at every fault. Prefer :meth:`diagflt_temporarily`.
        """
        self._cmd_checked(f"DIAGFLT {_one_of(mode, DIAGFLT_MODES, 'DIAGFLT')}")

    def diagflt_query(self) -> str:
        """``DIAGFLT?`` -> ``HALT``, ``ABORT`` or ``CONT``."""
        return _unquote(self.query("DIAGFLT?")).upper()

    @contextlib.contextmanager
    def diagflt_temporarily(self, mode: str):
        """Select a ``DIAGFLT`` mode for the body, then put the old one back.

        ``DIAGFLT`` is nonvolatile, so the restore runs on **every** exit
        path, exceptions included::

            with cal.diagflt_temporarily("HALT"):
                faults = cal.diag("D5700")      # stops at the first fault
                while cal.diag_halted():
                    faults += cal.diag_cont()   # step to the next

        Yields the previous mode, so a caller can report what it was.
        """
        previous = self.diagflt_query()
        self.diagflt(mode)
        try:
            yield previous
        finally:
            self.diagflt(previous)

    def self_test(self, *, timeout: float = SELF_TEST_TIMEOUT_SEC) -> bool:
        """``*TST?`` — run the self-tests. **True means the unit passed.**

        The instrument answers ``0`` for pass and ``1`` for fail; this returns
        the inverse, so ``if not cal.self_test():`` is the failure branch.
        That is one rule for the whole toolkit, learned
        once and then known everywhere.

        Any failures are logged into the **normal fault queue** as 4300-level
        codes (guide §9 ``*TST?``). They are deliberately left there:
        :meth:`drain_faults` is how you get them, with the offline decode from
        :data:`FAULT_CODES` if ``EXPLAIN?`` is unwell.

        ``*TST?`` is a **blocking** query with no documented duration and no
        non-blocking form (gap G9), so the controller read is simply held open
        for up to *timeout* — nothing else can use the bus meanwhile.
        :data:`SELF_TEST_TIMEOUT_SEC` is a guess; measure the real duration on
        the first run and pass it explicitly afterwards.
        """
        with self._temporary_timeout(timeout):
            resp = self.query("*TST?")
        result = _to_int(_unquote(resp), resp, "*TST? result")
        if result not in (0, 1):
            raise Fluke5700AProtocolError(
                f"*TST?: expected 0 (pass) or 1 (fail), got {resp!r}", resp)
        return result == 0

    def fatality(self) -> "list[str]":
        """``FATALITY?`` — the **fatal**-fault list. Service only (guide §16).

        A queue entirely separate from the one ``FAULT?`` serves, and not
        cleared by ``*CLS``. One entry per line, each
        ``<date> <time> <code> <description>``; returned verbatim because the
        field widths are never specified.

        .. note::
           An **empty** fatal-fault list cannot be told apart from firmware
           that does not know the command: gap G31 — an unknown query answers
           nothing at all — and the block reader reports zero lines as
           :class:`Fluke5700ACommError` rather than ``[]``, because zero lines
           is exactly what a dead link looks like too. So this raises after
           the block line timeout in both cases. Treat the exception as
           "no fatal-fault list available", not as "the bus is broken".
        """
        self._require_service("FATALITY?")
        return self.read_block("FATALITY?")

    def fatalclr(self) -> None:
        """``FATALCLR`` — clear the fatal-fault list. Service only (§16).

        The spelling is uncertain: [SM p.4-14] heads the entry ``FATLCLR?``
        while its own prose says ``FATALCLR`` three times (guide §16). This
        sends ``FATALCLR`` and falls back to ``FATLCLR`` if the calibrator
        answers fault :data:`FAULT_UNKNOWN_COMMAND`. Being a *command* rather
        than a query, a wrong spelling does produce that fault — unlike an
        unknown query, which produces no response at all (gap G31).
        """
        self._require_service("FATALCLR")
        for spelling in ("FATALCLR", "FATLCLR"):
            self.cmd(spelling)
            faults = self.drain_faults()
            others = [(code, text) for code, text in faults
                      if code != FAULT_UNKNOWN_COMMAND]
            if others:
                self._raise_for_faults(others, what=spelling)
            if len(others) == len(faults):
                return                      # no "Unknown Command": accepted
        raise Fluke5700AError(
            f"neither FATALCLR nor FATLCLR was accepted (fault "
            f"{FAULT_UNKNOWN_COMMAND}, Unknown Command). The fatal-fault list "
            f"cannot be cleared remotely on this firmware; guide §16 records "
            f"both spellings as uncertain.")

    # ── calibration (guide §7.6, §9, §14.6-§14.9) ─────────────────────

    def cal_temp(self, celsius: float) -> None:
        """``CAL_TEMP`` — ambient temperature for calibration activities.

        Must be sent **before** ``CAL_REF``, ``CAL_CHK``, ``CAL_WBFLAT`` or
        ``CAL_WBGAIN``; the value then applies to every calibration activity
        until changed. If it is never set the calibrator assumes **23.0 °C**
        (guide §9 ``CAL_TEMP``) — which is a real error source, not a
        formality, when the lab is not at 23 °C.

        Calibration is specified only over
        :data:`CAL_TEMP_SPEC_MIN_C`-:data:`CAL_TEMP_SPEC_MAX_C` (15-35 °C).
        That band is not enforced, but anything outside the instrument's
        0-50 °C operating range is refused: it is far more likely to be a
        ``230`` typed for ``23.0`` than a real reading.
        """
        try:
            value = float(celsius)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"CAL_TEMP takes a temperature in °C, got {celsius!r}"
            ) from exc
        if not math.isfinite(value) or not (
                CAL_TEMP_OPERATING_MIN_C <= value <= CAL_TEMP_OPERATING_MAX_C):
            raise ValueError(
                f"CAL_TEMP {value!r} °C is outside the calibrator's "
                f"{_fmt(CAL_TEMP_OPERATING_MIN_C)}-"
                f"{_fmt(CAL_TEMP_OPERATING_MAX_C)} °C operating range; "
                f"calibration is specified only over "
                f"{_fmt(CAL_TEMP_SPEC_MIN_C)}-{_fmt(CAL_TEMP_SPEC_MAX_C)} °C "
                f"(guide §11.6)")
        self._cmd_checked(f"CAL_TEMP {_fmt(value)}")

    def cal_temp_query(self, what: str = "CAL") -> float:
        """``CAL_TEMP?`` -> the temperature recorded for one activity.

        Selectors are :data:`CAL_TEMP_ACTIVITIES` — note that this command
        takes ``CHECK`` where :meth:`cal_days` and :meth:`cal_date` take
        ``ZERO`` (guide §9 ``CAL_TEMP?``).
        """
        what_u = _one_of(what, CAL_TEMP_ACTIVITIES, "CAL_TEMP? selector")
        resp = self.query(f"CAL_TEMP? {what_u}")
        return _to_float(_unquote(resp), resp, "CAL_TEMP? value")

    def cal_zero(self, *,
                 progress_cb: "Optional[Callable[[int, str], None]]" = None,
                 timeout: float = CAL_ZERO_TIMEOUT_SEC) -> None:
        """``CAL_ZERO`` — internal dc zeros calibration. Long-term.

        Removes offsets on the 2.2 V dc range. Takes about 2½ minutes, plus
        30 s with a 5725A attached, and is the one calibration activity that
        does **not** need the CALIBRATION switch (guide §9 ``CAL_ZERO``).

        Specifications require it every 30 days; otherwise fault 1303 is
        logged. Check with ``cal_days("ZERO")``.

        Two things measured on one unit, 2026-08-15, that the manual does
        not say — a run took 176.7 s and moved exactly nine constants:

        * **It zeros the dc current ranges too.** ``dcI zeros`` is a named
          phase, and ``I1Z``–``I5Z`` (220 µA … 2.2 A range zeros) all moved.
          [OM §4-4] lists only the 11 V / 2.2 V / 220 mV ranges plus the
          5725A's 11 A range, so its description is incomplete. The rest of
          the diff matched [OM §4-4] exactly: ``D1G``/``D1P`` (220 mV gain and
          offset) and ``D3P``/``D3M`` (11 V zeros).
        * **It does not write ``ZERO_TEMP``.** Sending :meth:`cal_temp` first
          does not change that; the constant kept the 2022 artifact-calibration
          value across two independent runs. Do not read it as the temperature
          of the last zero calibration.

        Note also that ``CAL_TEMP? CAL`` returns the *stored* per-activity
        temperature, not the live pending setting, so it cannot be used to read
        back a :meth:`cal_temp` just sent ([OM §5-24]).

        .. warning::
           Long-term command: **there is no remote abort** and ``STBY`` is
           rejected with fault 2002 while it runs (guide §19.2 gap G6). Do not
           start it with an output of 22 V or more live, and keep the
           front-panel STBY key within the operator's reach — it and line
           power are the only stops. See :meth:`_run_long_term`.
        """
        self._run_long_term("CAL_ZERO", progress_cb=progress_cb,
                            timeout=timeout)

    def cal_chk(self, *,
                progress_cb: "Optional[Callable[[int, str], None]]" = None,
                timeout: float = CAL_CHK_TIMEOUT_SEC) -> None:
        """``CAL_CHK`` — calibration check. Long-term, **about an hour**.

        Compares against the **internal** check standards and makes no
        permanent change to the calibration constants (guide §14.7), which
        makes it the safe periodic-confidence operation: no external standards,
        and **the rear-panel CALIBRATION switch is not needed** because nothing
        is stored ([OM §1-19], §7-12). Send :meth:`cal_temp` first — the
        ambient temperature is a required input, exactly as the front-panel
        "Cal Check" softkey prompts for it. Results come from
        :meth:`cal_slst`, :meth:`cal_shift` and :meth:`cal_rpt`, all with the
        ``CHECK`` group.

        **Duration: "Checking the calibration takes about an hour"**
        ([OM §1-19]) — this closes gap G10, which recorded only "longer than
        ``CAL_ZERO``". Hence :data:`CAL_CHK_TIMEOUT_SEC` rather than the
        generic 30-minute long-term default, which would expire mid-check.

        Warm-up matters for the result, not just for the specification:
        [OM §4-3] wants **30 minutes** if the calibrator has been off for an
        hour or more, or twice the off-time below that.

        .. warning::
           Long-term command: **there is no remote abort** and ``STBY`` is
           rejected with fault 2002 while it runs (guide §19.2 gap G6). Do not
           start it with an output of 22 V or more live, and keep the
           front-panel STBY key within the operator's reach.
        """
        self._run_long_term("CAL_CHK", progress_cb=progress_cb,
                            timeout=timeout)

    def cal_wbgain(self, step: str, *,
                   progress_cb: "Optional[Callable[[int, str], None]]" = None,
                   timeout: float = LONG_TERM_TIMEOUT_SEC) -> None:
        """``CAL_WBGAIN`` — wideband (Option 5700A-03) gain calibration.

        Done every calibration cycle ([OM Chapter 7]). The wideband output
        cable connects to the calibrator's own SENSE binding posts — center
        conductor to SENSE HI for ``PGAIN``, to SENSE LO for ``NGAIN`` — and
        the sequence is::

            cal.cal_temp(23.5)
            cal.cal_wbgain("PGAIN")
            # reverse the SENSE connection now
            cal.cal_wbgain("NGAIN")
            cal.cal_wbgain("STORE")     # CALIBRATION switch in ENABLE

        ``PGAIN``/``NGAIN`` are long-term (no remote abort; ``STBY`` is
        rejected with fault 2002 while they run). ``STORE`` writes EEPROM and
        needs the rear-panel CALIBRATION switch; the refusal is fault 845,
        raised here as :class:`CalibrationSwitchError`.
        """
        step_u = _one_of(step, ("PGAIN", "NGAIN", "STORE"),
                         "CAL_WBGAIN step")
        if step_u == "STORE":
            self.cmd("CAL_WBGAIN STORE;*WAI")
            self._raise_for_faults(self.drain_faults(),
                                   what="CAL_WBGAIN STORE")
            return
        self._run_long_term(f"CAL_WBGAIN {step_u}", progress_cb=progress_cb,
                            timeout=timeout)

    def cal_wbflat(self, step: str) -> None:
        """``CAL_WBFLAT`` — wideband flatness calibration, one step.

        Recommended every two years, during full verification ([SM
        Chapter 3]). Not a long-term command: each step returns as soon as
        the calibrator accepts it. The documented loop (guide §9)::

            cal.cal_temp(24.6)
            cal.cal_wbflat("START")     # sources the first flatness point
            cal.operate()
            cal.incr(...)               # null against the external standard
            cal.cal_wbflat("NULL")      # compute constant, source next point
            # ... repeat INCR/NULL per point; OUT? returning 0,V,0 means done
            cal.cal_wbflat("STORE")     # CALIBRATION switch in ENABLE

        Sequencing and the termination test belong to the procedure layer;
        this method exposes single steps. Related faults: 842 (Not A Valid
        Wideband Flatness Point), 843 (Wideband Flatness Cal Is Not Active),
        845 on ``STORE`` without the switch (raised as
        :class:`CalibrationSwitchError`).
        """
        step_u = _one_of(step, ("START", "NULL", "STORE"), "CAL_WBFLAT step")
        if step_u == "STORE":
            self.cmd("CAL_WBFLAT STORE;*WAI")
            self._raise_for_faults(self.drain_faults(),
                                   what="CAL_WBFLAT STORE")
            return
        self.cmd(f"CAL_WBFLAT {step_u}")
        self._raise_for_faults(self.drain_faults(),
                               what=f"CAL_WBFLAT {step_u}")

    def cal_ref(self, value: float, unit: str, *,
                progress_cb: "Optional[Callable[[int, str], None]]" = None,
                timeout: float = LONG_TERM_TIMEOUT_SEC) -> None:
        """``CAL_REF`` — one artifact step of the main-output calibration.

        *value* is the **true value of the external standard** on the binding
        posts and *unit* one of :data:`CAL_REF_UNITS`. The value and its units
        are what tell the calibrator which procedure to run (guide §9
        ``CAL_REF``), so ``KOHM`` is accepted verbatim — the manual's own
        10 kΩ step is written ``CAL_REF 9.99987 KOHM``.

        **The four steps have a mandatory order** (guide §14.6)::

            cal.cal_temp(23.5)
            cal.cal_ref( 10.00013, "V")      # 1. +10 V
            cal.cal_ref(-10.00013, "V")      # 2. -10 V, leads reversed
            cal.cal_ref(  9.99987, "KOHM")   # 3. 10 kOhm
            cal.cal_ref(  1.00026, "OHM")    # 4. 1 Ohm
            cal.cal_adj()
            cal.cal_store()                  # CALIBRATION switch in ENABLE

        Out of order gives fault :data:`FAULT_CAL_STEPS_OUT_OF_ORDER` (852).
        **This method exposes a single step and does not enforce the
        sequence** — sequencing, operator prompting and the artifact values
        belong to the calibration-procedure layer, which is the only place
        that knows which standard is actually connected.

        Omitting :meth:`cal_store` at the end collects performance data
        without committing any constant.

        .. warning::
           Long-term command: **there is no remote abort** and ``STBY`` is
           rejected with fault 2002 while it runs (guide §19.2 gap G6). The
           external standard is on the binding posts throughout, so make sure
           the calibrator is not sourcing 22 V or more when the step starts,
           and keep the front-panel STBY key within the operator's reach.
        """
        unit_u = _one_of(unit, CAL_REF_UNITS, "CAL_REF unit")
        try:
            magnitude = float(value)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"CAL_REF takes the true value of the standard as a number, "
                f"got {value!r}") from exc
        if not math.isfinite(magnitude):
            raise ValueError(f"CAL_REF value {value!r} is not finite")
        self._run_long_term(f"CAL_REF {magnitude:.10E} {unit_u}",
                            progress_cb=progress_cb, timeout=timeout)

    def cal_adj(self, *,
                progress_cb: "Optional[Callable[[int, str], None]]" = None,
                timeout: float = LONG_TERM_TIMEOUT_SEC) -> None:
        """``CAL_ADJ`` — the internal adjustment. Long-term.

        Send after the last :meth:`cal_ref` step; nothing is committed to
        nonvolatile memory until :meth:`cal_store`.

        No duration is documented (gap G10). Note that this is precisely the
        operation a leaked block-read timeout breaks: :meth:`read_block`
        always restores the session timeout for that reason.

        .. warning::
           The longest long-term command, and **there is no remote abort**:
           ``STBY`` is rejected with fault 2002 while it runs (guide §19.2
           gap G6). Do not start it with an output of 22 V or more live, and
           keep the front-panel STBY key within the operator's reach — it and
           line power are the only stops.
        """
        self._run_long_term("CAL_ADJ", progress_cb=progress_cb,
                            timeout=timeout)

    def cal_store(self) -> None:
        """``CAL_STORE`` — commit the new constants to nonvolatile memory.

        **Requires the rear-panel CALIBRATION switch in ENABLE.** There is no
        remote query for the switch (guide §7.6, gap G29), so the refusal is
        detected from fault 845 and raised as a
        :class:`CalibrationSwitchError` that says so in words.

        ``*WAI`` is appended so the ``FAULT?`` drain that follows cannot
        overtake the EEPROM write, matching the guide §14.6 recipe.
        """
        self.cmd("CAL_STORE;*WAI")
        self._raise_for_faults(self.drain_faults(), what="CAL_STORE")

    def cal_rng(self, arg: str, magnitude: Optional[float] = None,
                frequency: Optional[float] = None) -> None:
        """``CAL_RNG`` — range calibration against an external standard.

        *arg* is a §10 range identifier, or ``NULL`` (accept the present
        nulled output and compute the constant) or ``STORE`` (commit — this
        needs the CALIBRATION switch in ENABLE).

        With a range identifier, *magnitude* is mandatory and must be
        **45 %-95 % of that range** (guide §9 ``CAL_RNG``); it is checked
        offline by :func:`validate_cal_rng` and raises :class:`ValueError`
        before any I/O, because the alternative is faults 847/848 mid-way
        through a calibration. The unit is derived from the range identifier
        (:func:`cal_rng_unit`), never synthesized with a multiplier prefix.

        The full sequence (guide §14.8)::

            cal.cal_temp(23.5)
            cal.cal_rng("DC2_2V", 2.0); cal.operate()
            ...                          # INCR until the standard nulls
            cal.cal_rng("NULL")
            cal.cal_rng("STORE")

        ``CAL_RNG`` is *not* a long-term command, so it returns as soon as the
        calibrator accepts it.
        """
        arg_u = str(arg).strip().upper()
        if arg_u in ("NULL", "STORE"):
            if magnitude is not None or frequency is not None:
                raise ValueError(
                    f"CAL_RNG {arg_u} takes no magnitude or frequency")
            self.cmd(f"CAL_RNG {arg_u}")
            if arg_u == "NULL":
                # NULL accepts "the present nulled output" -- whatever the
                # INCR nulling left, which this driver did not command.
                self._forget_output()
            self._raise_for_faults(self.drain_faults(),
                                   what=f"CAL_RNG {arg_u}")
            return

        if arg_u not in RANGE_IDS:
            raise ValueError(
                f"unknown CAL_RNG argument {arg!r}: expected a guide §10 "
                f"range identifier, or the keyword NULL or STORE. Refusing "
                f"to pass it through: an unrecognized range must never reach "
                f"the instrument as though it were checked")
        if magnitude is None:
            raise ValueError(
                f"CAL_RNG {arg_u} needs a calibration magnitude "
                f"({CAL_RNG_MIN_FRACTION:.0%}-{CAL_RNG_MAX_FRACTION:.0%} of "
                f"the range)")
        problems = validate_cal_rng(arg_u, magnitude)
        if problems:
            raise ValueError(
                f"refusing to start a range calibration of {arg_u} at "
                f"{magnitude!r}: " + "; ".join(problems))

        command = (f"CAL_RNG {arg_u}, {float(magnitude):.10E} "
                   f"{cal_rng_unit(arg_u)}")
        if frequency is not None:
            command += f", {float(frequency):.6E} HZ"
        self.cmd(command)
        # CAL_RNG stages an output the way OUT does -- the §14.8 recipe is
        # ``cal_rng("DC1100V", 1000.0); operate()`` -- so record it, or
        # standby() would size a kilovolt teardown from a stale setpoint.
        self._staged_output = (float(magnitude), cal_rng_unit(arg_u))
        self._staged_hz = float(frequency) if frequency is not None else 0.0
        self._raise_for_faults(self.drain_faults(), what=command)

    # ── calibration reports and constants (guide §9, §14.7) ───────────

    def cal_rpt(self, which: str = "CAL") -> str:
        """``CAL_RPT?`` -> the formatted report, **as raw text**.

        Returned unparsed on purpose: the report format is delegated by the
        manuals to [OM Chapter 6], which describes its content but gives no
        machine-parseable grammar (gap G24), so the parser has to be written
        against a real captured report and lives in one place — not
        duplicated here.
        """
        which_u = _one_of(which, CAL_REPORT_GROUPS, "CAL_RPT? group")
        return "\n".join(self.read_block(f"CAL_RPT? {which_u}"))

    def cal_slst(self, which: str = "CAL") -> "list[RangeShift]":
        """``CAL_SLST?`` -> the output shifts for **every** range."""
        which_u = _one_of(which, CAL_SHIFT_GROUPS, "CAL_SLST? group")
        return parse_shift_list(self.read_block(f"CAL_SLST? {which_u}"))

    def cal_shift(self, which: str, range_id: str) -> RangeShift:
        """``CAL_SHIFT?`` -> the output shifts for one §10 range.

        *which* is ``CAL`` (shifts due to calibration) or ``CHECK`` (due to a
        calibration check) — guide §13 item 25 records that the OM's own gloss
        of ``CAL`` is wrong.
        """
        which_u = _one_of(which, CAL_SHIFT_GROUPS, "CAL_SHIFT? group")
        range_u = str(range_id).strip().upper()
        if range_u not in RANGE_IDS:
            raise ValueError(
                f"unknown range identifier {range_id!r} for CAL_SHIFT?; "
                f"expected a guide §10 identifier")
        return parse_range_shift(
            self.read_block(f"CAL_SHIFT? {which_u}, {range_u}"))

    def cal_clst(self, which: str = "CAL") -> "dict[str, float]":
        """``CAL_CLST?`` -> every calibration constant, as ``{name: value}``.

        Roughly 400 pairs (gap G2). This is also how guide §10 recommends
        discovering the firmware's real symbolic names instead of hard-coding
        [OM Appendix C].
        """
        which_u = _one_of(which, CAL_CONSTANT_GROUPS, "CAL_CLST? group")
        return parse_constant_list(self.read_block(f"CAL_CLST? {which_u}"))

    def cal_const(self, which: str, name: str) -> float:
        """``CAL_CONST?`` -> one calibration constant by symbolic name.

        Names are in [OM Appendix C] — the OM's own pointer to "Appendix D" is
        a misprint (guide §13 item 26). Examples: ``KV6`` (6.5 V reference
        true value), ``D3P`` (11 V range positive zero). :meth:`cal_clst`
        enumerates the live set.
        """
        which_u = _one_of(which, CAL_CONSTANT_GROUPS, "CAL_CONST? group")
        constant = str(name).strip()
        if not constant:
            raise ValueError("CAL_CONST? needs a constant name")
        resp = self.query(f"CAL_CONST? {which_u}, {constant}")
        return _to_float(_unquote(resp), resp,
                         f"CAL_CONST? {which_u},{constant}")

    def ohms_ref(self, which: str = "CAL") -> float:
        """``OHMSREF?`` -> the calculated resistance reference.

        The average actual-to-nominal ratio of the 100 Ω … 190 kΩ resistors,
        as printed on the calibration report. **Requires Main CPU software
        revision H or later**, which ``*IDN?`` cannot tell you (gap G30) — so
        this is a feature probe.

        Two of three sources spell it ``OHMSREF?`` and one ``OHMREF?``
        (guide §13 item 1), so both are tried. Because an unknown *query*
        produces **no response at all** rather than a fault (gap G31), the
        first attempt uses the short :data:`PROBE_TIMEOUT_SEC` and a timeout
        is taken as the negative result; a fault
        :data:`FAULT_UNKNOWN_COMMAND` counts as one too.
        """
        which_u = _one_of(which, CAL_CONSTANT_GROUPS, "OHMSREF? group")
        last_error: Optional[Exception] = None
        for spelling in ("OHMSREF?", "OHMREF?"):
            try:
                with self._temporary_timeout(PROBE_TIMEOUT_SEC):
                    resp = self.query(f"{spelling} {which_u}")
            except Fluke5700ACommError as exc:
                last_error = exc
                # The probe itself is the information; whatever the misspelled
                # query left in the queue is noise on the next command.
                self.drain_faults()
                continue
            faults = self.drain_faults()
            if any(code == FAULT_UNKNOWN_COMMAND for code, _ in faults):
                continue
            self._raise_for_faults(faults, what=spelling)
            return _to_float(_unquote(resp), resp, f"{spelling} value")
        raise Fluke5700AError(
            f"neither OHMSREF? nor OHMREF? answered. The command needs Main "
            f"CPU software revision H or later (guide §9 OHMSREF?), and an "
            f"unknown query returns nothing at all rather than faulting "
            f"(gap G31), so this is most likely older firmware."
        ) from last_error

    def cal_days(self, what: str = "CAL") -> int:
        """``CAL_DAYS?`` -> days since the last activity of that type.

        Selectors are :data:`CAL_ACTIVITIES`. ``cal_days("ZERO")`` is the
        30-day dc-zeros gate of guide §11.6.
        """
        what_u = _one_of(what, CAL_ACTIVITIES, "CAL_DAYS? selector")
        return self.query_int(f"CAL_DAYS? {what_u}")

    def cal_date(self, what: str = "CAL") -> str:
        """``CAL_DATE?`` -> the date of the last activity, as six digits.

        Returned as a zero-padded 6-character string rather than a ``date``
        because the field order is whatever ``DATEFMT`` says — ``MMDDYY``,
        ``DDMMYY`` or ``YYMMDD``. **Call :meth:`datefmt` before interpreting
        it**, and note gap G25: the two-digit year has no century window.
        """
        what_u = _one_of(what, CAL_ACTIVITIES, "CAL_DATE? selector")
        resp = self.query(f"CAL_DATE? {what_u}")
        return _six_digits(_unquote(resp), "CAL_DATE?", resp)

    def cal_intv(self) -> int:
        """``CAL_INTV?`` -> the calibration interval in days."""
        return self.query_int("CAL_INTV?")

    def set_cal_intv(self, days: int) -> None:
        """``CAL_INTV`` — set the calibration interval (1/90/180/365 days).

        .. warning::
           Saved in **nonvolatile memory** and used to compute ``UNCERT?``.
           Changing it changes what the calibrator reports as its own
           specification, for every future session and for the front panel —
           it is a statement about the calibration programme, not a display
           preference.
        """
        try:
            value = int(days)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"CAL_INTV takes a number of days, got {days!r}") from exc
        if value not in CAL_INTERVALS_DAYS:
            raise ValueError(
                f"CAL_INTV must be one of "
                f"{', '.join(str(d) for d in CAL_INTERVALS_DAYS)} days; got "
                f"{days!r}")
        self._cmd_checked(f"CAL_INTV {value}")

    def cal_conf(self) -> str:
        """``CAL_CONF?`` -> ``CONF95`` or ``CONF99``."""
        return _unquote(self.query("CAL_CONF?")).upper()

    def set_cal_conf(self, level) -> None:
        """``CAL_CONF`` — set the specification confidence level.

        Accepts ``CONF95``/``CONF99`` or plain ``95``/``99``, and always sends
        the ``CONF``-prefixed form: guide §13 item 5 records that the OM and
        the RPR disagree, and recommends the OM's spelling because it matches
        what ``CAL_CONF?`` answers.

        .. warning::
           Like :meth:`set_cal_intv` this persists across power cycles and
           changes what ``UNCERT?`` returns — the same 10 V output will be
           quoted a different uncertainty afterwards.
        """
        token = str(level).strip().upper()
        if token in ("95", "99"):
            token = f"CONF{token}"
        self._cmd_checked(
            f"CAL_CONF {_one_of(token, CAL_CONF_LEVELS, 'CAL_CONF level')}")

    # ── housekeeping (guide §14.11) ───────────────────────────────────

    def etime(self) -> int:
        """``ETIME?`` -> total minutes powered on for the life of the unit."""
        return self.query_int("ETIME?")

    def set_etime(self, minutes: int) -> None:
        """``ETIME SET_TO`` — set the lifetime counter. Service only (§16).

        Used after replacing the A20 CPU assembly, the clock battery BT1 or
        the clock/calendar IC.
        """
        self._require_service("ETIME SET_TO")
        try:
            value = int(minutes)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                f"ETIME SET_TO takes minutes, got {minutes!r}") from exc
        if not 0 <= value <= 2147483647:
            raise ValueError(
                f"ETIME SET_TO takes 0 … 2147483647 minutes, got {value}")
        self._cmd_checked(f"ETIME SET_TO, {value}")

    def ontime(self) -> int:
        """``ONTIME?`` -> minutes since power-up (**this session**).

        The warm-up gate. Specifications require "twice the time since last
        warmed up, to a maximum of :data:`WARMUP_MAX_MINUTES` minutes"
        (guide §11.6) — so a unit that has been off for 5 minutes needs 10,
        and one that has been off overnight needs the full 30. The
        off-duration is not queryable, so a caller that cannot know it should
        simply require :data:`WARMUP_MAX_MINUTES`.
        """
        return self.query_int("ONTIME?")

    def clock(self) -> "tuple[str, str]":
        """``CLOCK?`` -> ``(hhmmss, date)``, both zero-padded to six digits.

        The date's field order follows :meth:`datefmt`. Both are strings
        because 08:00:00 comes back as the integer ``80000`` (gap G25).
        """
        return parse_clock(self.query("CLOCK?"))

    def set_clock(self, time_hhmmss, date) -> None:
        """``CLOCK`` — set the clock/calendar.

        *time_hhmmss* is ``HHMMSS`` and *date* is in whatever order
        :meth:`datefmt` reports. Both accept a string or an integer and are
        zero-padded to six digits.

        **Requires the CALIBRATION switch in ENABLE**; refusal (fault 838)
        raises :class:`CalibrationSwitchError`.
        """
        time_field = _six_digits(time_hhmmss, "CLOCK time", str(time_hhmmss))
        date_field = _six_digits(date, "CLOCK date", str(date))
        command = f"CLOCK {time_field}, {date_field}"
        self.cmd(command)
        self._raise_for_faults(self.drain_faults(), what="CLOCK")

    def datefmt(self) -> str:
        """``DATEFMT?`` -> ``MDY``, ``DMY`` or ``YMD``.

        Query this before interpreting anything from :meth:`clock` or
        :meth:`cal_date`.
        """
        return _unquote(self.query("DATEFMT?")).upper()

    def set_datefmt(self, fmt: str) -> None:
        """``DATEFMT`` — the date order used by ``CLOCK``/``CAL_DATE?``."""
        self._cmd_checked(f"DATEFMT {_one_of(fmt, DATE_FORMATS, 'DATEFMT')}")

    def rpt_str(self) -> str:
        """``RPT_STR?`` -> the user report string."""
        return _unquote(self.query("RPT_STR?"))

    def set_rpt_str(self, text: str) -> None:
        """``RPT_STR`` — set the user report string (≤ 40 characters).

        Appears on calibration reports and on the Control Display.
        **Requires the CALIBRATION switch in ENABLE** (fault 855); over-long
        strings are rejected offline rather than by fault 844.
        """
        value = str(text)
        if len(value) > RPT_STR_MAX_CHARS:
            raise ValueError(
                f"the report string may be at most {RPT_STR_MAX_CHARS} "
                f"characters (fault 844); got {len(value)}")
        if '"' in value:
            raise ValueError(
                "the report string cannot contain a double quote: the "
                "manuals document no escape for it (gap G13)")
        self.cmd(f'RPT_STR "{value}"')
        self._raise_for_faults(self.drain_faults(), what="RPT_STR")

    def pud(self) -> str:
        """``*PUD?`` -> the protected user data (empty when never set)."""
        return parse_pud(self._query_raw("*PUD?"))

    def set_pud(self, data: str) -> None:
        """``*PUD`` — store up to 63 bytes in nonvolatile memory.

        Sent in the counted form, ``*PUD #<n><length><data>``: ``"FLUKE"``
        goes out as ``*PUD #15FLUKE``. The alternative form is terminated by
        an LF with EOI, which a line-oriented transport cannot express.

        **Requires the CALIBRATION switch in ENABLE.**
        """
        payload = str(data)
        try:
            encoded = payload.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(
                f"*PUD stores ASCII bytes only, got {payload!r}") from exc
        if len(encoded) > PUD_MAX_BYTES:
            raise ValueError(
                f"*PUD holds at most {PUD_MAX_BYTES} bytes, got "
                f"{len(encoded)}")
        if any(byte < 32 for byte in encoded):
            raise ValueError(
                "*PUD data cannot contain control characters (guide §3.9)")
        digits = str(len(encoded))
        self.cmd(f"*PUD #{len(digits)}{digits}{payload}")
        self._raise_for_faults(self.drain_faults(), what="*PUD")

    def echo(self, text: str) -> str:
        """``ECHO?`` — loop a string back through the interface.

        The cheapest possible end-to-end link test, and a good first command
        for a service console: it exercises the write path, the read path and
        the quoting without touching the instrument's state at all. Case is
        preserved by the calibrator, so a mismatch is meaningful.
        """
        value = str(text)
        if '"' in value:
            raise ValueError(
                "ECHO? cannot carry a double quote: the manuals document no "
                "escape for it (gap G13)")
        return _unquote(self.query(f'ECHO? "{value}"'))

    def lrn(self) -> str:
        """``*LRN?`` -> a command string that recreates the present settings.

        Captures the output value, guard, sense, limits, wideband, boost,
        range lock, two-wire compensation, phase lock/shift, offset, scale —
        **and Operate/standby**.

        .. warning::
           That last one is why this driver has no ``restore()`` convenience.
           Writing the returned string back **re-establishes Operate**, so a
           blind replay can energize the output — at up to 1100 V — without
           anything in the calling code looking like it turned the output on
           (guide §14.10). If you replay it, send ``STBY`` first, inspect the
           string, and prefer explicit reconstruction for anything above 22 V.

        Returned verbatim, not unquoted: it is meant to go back on the wire
        exactly as it arrived.
        """
        return self.query("*LRN?")

    def __enter__(self) -> "Fluke5700A":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

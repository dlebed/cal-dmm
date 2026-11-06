from caldmm.drivers import gpib as Gpib  # linux-gpib or VISA, see drivers/gpib.py
import re
import time
from enum import Enum, IntEnum

from caldmm.drivers._base import MultimeterBase, WorkerBase

# Empirically measured trigger-to-data-available latency on an HP 3458A
# at 50 Hz line, DCV 10, AZERO ON, OCOMP ON, DELAY 0.25, INBUF ON.
# 6 samples per NPLC, stddev <30 ms. Use median for fast settings (skip
# polling overhead) and max for slow ones (pessimistic; sub-second
# precision doesn't matter when the conversion is multi-second).
_EXPECTED_CONVERSION_SEC = {
    1:   0.10,   # extrapolated; not characterized
    10:  0.62,
    20:  0.97,
    50:  2.04,
    100: 3.70,
    200: 7.06,
}


def _expected_conversion_sec(nplc: int) -> float:
    """Return expected conversion time for the given NPLC, falling back to
    the closest tabulated value if NPLC is outside the table."""
    if nplc in _EXPECTED_CONVERSION_SEC:
        return _EXPECTED_CONVERSION_SEC[nplc]
    keys = sorted(_EXPECTED_CONVERSION_SEC)
    if nplc < keys[0]:
        return _EXPECTED_CONVERSION_SEC[keys[0]]
    if nplc > keys[-1]:
        return _EXPECTED_CONVERSION_SEC[keys[-1]]
    # Linear interpolation between bracketing entries
    lo = max(k for k in keys if k <= nplc)
    hi = min(k for k in keys if k >= nplc)
    if lo == hi:
        return _EXPECTED_CONVERSION_SEC[lo]
    frac = (nplc - lo) / (hi - lo)
    return (_EXPECTED_CONVERSION_SEC[lo]
            + frac * (_EXPECTED_CONVERSION_SEC[hi] - _EXPECTED_CONVERSION_SEC[lo]))


# How much longer one *resistance* reading takes with OCOMP ON. The table
# above was characterized on DCV, where OCOMP does nothing, so it describes
# plain integration only.
#
# The programming guide's OCOMP entry (docs/3458a/3458A_Programming_Guide.md
# :238-239) says offset compensation "doubles read time". That is not what
# a bench measurement shows: OHMF 10 Ohm range, NPLC 100,
# AZERO ON, 19 Ohm sourced by a Fluke 5700A, 4-wire:
#
#     OCOMP OFF   one reading    18.998980 Ohm     3.69 s
#     OCOMP ON    one reading    18.999116 Ohm    23.17 s
#
# 23.17 / 3.69 = 6.28, and 3.69 s matches _expected_conversion_sec(100) =
# 3.70 s to 0.3 %, so the discrepancy is entirely OCOMP's. The extra time is
# consistent with offset compensation running the whole AZERO-interleaved
# measurement pair twice and settling the current source in between, but the
# manual documents no such breakdown, so this stays what it is:
#
#   THIS IS A BENCH MEASUREMENT, NOT A DATASHEET FIGURE. One configuration
#   (NPLC 100), one range, one unit. It is rounded up from 6.28 to leave
#   headroom, and every caller multiplies it by a further safety factor
#   rather than treating it as exact.
_OCOMP_TIME_FACTOR = 6.5


class Func(Enum):
    DCV = 1
    ACV = 2
    ACDCV = 3
    OHM = 4
    OHMF = 5
    DCI = 6
    ACI = 7
    ACDCI = 8
    FREQ = 9
    PER = 10
    DSAC = 11
    DSDC = 12
    SSAC = 13
    SSDC = 14

    def __str__(self):
        return self.name

    @classmethod
    def get_unit(cls, func: 'Func') -> str:
        units = {
            cls.DCV: 'V', cls.ACV: 'V', cls.ACDCV: 'V',
            cls.OHM: 'Ohm', cls.OHMF: 'Ohm',
            cls.DCI: 'A', cls.ACI: 'A', cls.ACDCI: 'A',
            cls.FREQ: 'Hz', cls.PER: 's',
            cls.DSAC: '', cls.DSDC: '', cls.SSAC: '', cls.SSDC: ''
        }

        return units.get(func, '')

    @property
    def unit(self) -> str:
        return Func.get_unit(self)


class ACAL(Enum):
    ALL = 0
    DCV = 1
    AC = 2
    OHMS = 4

    def __str__(self):
        return self.name

    @classmethod
    def get_duration(cls, func) -> int:
        """How long ``ACAL <type>`` runs, in seconds.

        **Every one of these figures was previously short**, by 15 s to 2 min.
        ``acal()`` sleeps this and returns, so a caller that measured
        immediately afterwards was measuring a meter still autocalibrating —
        which is what ``tools/dmm_noise.py``, ``apps/dmm_bias.py`` and
        ``apps/kvd_linearity.py`` all did with ``ACAL DCV``.

        The authority is the *Keysight 3458A Calibration Manual* (03458-90017),
        ``ACAL`` command reference p.87, "The approximate time required to
        perform each autocal routine":

            ALL: 16 minutes;  DCV: 2 minutes and 45 seconds;
            AC:  2 minutes and 45 seconds;  OHMS: 11 minutes

        Note p.45 step 7 of the adjustment procedure says ``ACAL AC`` takes
        "about 2 minutes" — the manual contradicting itself. p.87's table is
        the specific claim and the longer one, so it wins.

        This is the *run* time, not the settling time. p.87 separately asks for
        30 / 15 / 15 / 30 minutes of thermal settling afterwards "to obtain
        optimal performance"; that is the caller's business, not this method's.
        """
        match func:
            case cls.ALL:
                return 16 * 60
            case cls.DCV:
                return 2 * 60 + 45
            case cls.AC:
                return 2 * 60 + 45
            case cls.OHMS:
                return 11 * 60

        return 0

    @property
    def duration(self) -> int:
        return ACAL.get_duration(self)


class CalItem(IntEnum):
    Nominal = 0
    Actual = 1
    UpperLimit = 3
    LowerLimit = 5

    def __str__(self):
        return self.name


# ── External calibration adjustment (the CAL command) ─────────────────────
#
# Sources: *Keysight 3458A Calibration Manual* — "Calibration Security" (p.21),
# "Hardware lock-out of calibration" (p.23), the ``CAL`` command reference
# (p.87-89) and the DC-gain / resistance adjustment procedures (ch.3); plus
# the *3458A User's Guide* ``SECURE`` entry (p.339-341) and the error-register
# tables mirrored in ``docs/3458a/3458A_Programming_Guide.md`` §12.
#
# **ACAL is not this.** ``ACAL`` (:meth:`HP3458A.acal`) is *auto-calibration*:
# the meter measuring its own internal 7 V reference and 40 kOhm reference and
# recomputing gain/offset constants from them. It needs no external equipment,
# is run daily, and cannot improve absolute accuracy beyond what the internal
# references already hold. ``CAL`` is *external adjustment*: it retrains those
# internal references against a traceable standard on the terminals, and it is
# the only one of the two that can move the meter's absolute accuracy. See
# :meth:`HP3458A.adjust`.
#
# The 34401A's calibration is gated by a SCPI security code. The 3458A's is
# gated by two quite different things — see :class:`CalibrationLockoutError`.

#: Documented adjustment points (Calibration Manual p.87-89, ch.3):
#:
#: * ``CAL 0``     — offset / "DC zeros", with a four-wire low-thermal short.
#:                   Must be done twice, once per Terminals-switch position.
#: * ``CAL <V>``   — DC gain, 1 V to 12 V; 10 V recommended.
#: * ``CAL <ohm>`` — resistance and DC current gain, 1 kOhm to 12 kOhm;
#:                   10 kOhm recommended.
#:
#: Anything outside these bands is refused locally. A standard below the
#: recommended value is accepted but degrades the meter's specifications.
CAL_VOLTAGE_MIN = 1.0
CAL_VOLTAGE_MAX = 12.0
CAL_RESISTANCE_MIN = 1.0e3
CAL_RESISTANCE_MAX = 12.0e3

#: Approximate durations from the Calibration Manual's procedures: DC gain
#: "about 2 minutes" (p.42), resistance/DCI "about 12 minutes" (p.45), each
#: offset pass "about 5 minutes" (p.40-41). Used to size the default wait;
#: generous, because finishing early costs nothing and polling early risks
#: reading a half-written state.
CAL_EXPECTED_SECONDS = {
    'offset': 5 * 60,
    'dc_gain': 2 * 60,
    'resistance': 12 * 60,
}

# Error register (ERR?) bit weights — programming guide §12.
ERR_HARDWARE = 1
ERR_CALIBRATION = 2
ERR_SYNTAX = 8
ERR_UNDEFINED_PARAMETER = 32
ERR_PARAMETER_OUT_OF_RANGE = 64
ERR_OUT_OF_CALIBRATION = 512
ERR_CALIBRATION_REQUIRED = 1024

# Auxiliary error register (AUXERR?) bit weights — programming guide §12.
AUXERR_CAL_VALUE_OUT_OF_RANGE = 32
AUXERR_CAL_RAM_WRITE_PROTECTION = 16384

#: Accepted shape of a ``SECURE`` code: alphanumeric with an optional leading
#: minus. Deliberately excludes whitespace, control characters, ``,`` and
#: ``;`` — the 3458A accepts multiple commands on one line, so an unvalidated
#: code could append a second command to an irreversible calibration write.
_SECURITY_CODE_RE = re.compile(r'^-?[A-Za-z0-9]+$')

_ERR_BIT_NAMES = {
    1: 'hardware (see AUXERR?)',
    2: 'calibration error',
    4: 'trigger too fast',
    8: 'syntax',
    16: 'command not allowed from remote',
    32: 'undefined parameter',
    64: 'parameter out of range',
    128: 'memory error',
    256: 'destructive overload detected',
    512: 'out of calibration',
    1024: 'calibration required',
    2048: 'settings conflict',
    4096: 'math error',
    8192: 'subprogram error',
    16384: 'system error',
}

_AUXERR_BIT_NAMES = {
    1: 'slave processor not responding',
    2: 'DTACK failure',
    4: 'slave processor self-test failure',
    8: 'isolator test failure',
    16: 'A/D converter convergence failure',
    32: 'calibration value out of range',
    64: 'GPIB chip failure',
    128: 'UART failure',
    256: 'timer failure',
    512: 'internal overload',
    1024: 'ROM checksum (low)',
    2048: 'ROM checksum (high)',
    4096: 'nonvolatile RAM failure',
    8192: 'option RAM failure',
    16384: 'cal RAM write/protection failure',
}


def _decode_error_bits(value: int, names: dict) -> str:
    """Render a weighted-sum error register as readable text."""
    if not value:
        return 'none'
    parts = [text for weight, text in sorted(names.items()) if value & weight]
    return ', '.join(parts) or f'unknown bits (0x{value:x})'


def adjustment_kind(value: float) -> str:
    """Classify a ``CAL`` argument as ``offset``/``dc_gain``/``resistance``.

    Pure function, so a procedure can be checked with no instrument attached.
    Raises :class:`ValueError` for anything the Calibration Manual does not
    document as a legal ``CAL`` argument.
    """
    try:
        value = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'CAL value must be a number, got {value!r}')
    if value != value or value in (float('inf'), float('-inf')):
        raise ValueError(f'CAL value must be finite, got {value!r}')
    if value == 0:
        return 'offset'
    if CAL_VOLTAGE_MIN <= value <= CAL_VOLTAGE_MAX:
        return 'dc_gain'
    if CAL_RESISTANCE_MIN <= value <= CAL_RESISTANCE_MAX:
        return 'resistance'
    raise ValueError(
        f'CAL {value!r} is outside every documented adjustment band. The '
        f'3458A Calibration Manual (p.87-89) allows CAL 0 (offset, with a '
        f'four-wire short), CAL {CAL_VOLTAGE_MIN}..{CAL_VOLTAGE_MAX} V (DC '
        f'gain, 10 V recommended) and CAL {CAL_RESISTANCE_MIN:g}..'
        f'{CAL_RESISTANCE_MAX:g} Ohm (resistance/DCI, 10 kOhm recommended). '
        f'Nothing was sent.')


def expected_adjustment_seconds(value: float) -> int:
    """Documented duration of ``CAL <value>``, in seconds."""
    return CAL_EXPECTED_SECONDS[adjustment_kind(value)]


#: What to tell an operator whose ``CAL`` was refused. Neither gate can be
#: queried remotely, so this text is the entire user experience of the
#: failure; it is attached to every calibration error, not just the one bit
#: that is *probably* the lockout, because the mapping is undocumented.
_LOCKOUT_ADVICE = (
    'Calibration access on the 3458A is gated by two things, and NEITHER can '
    'be queried remotely: (1) the security code set with SECURE — pass it as '
    'security_code=, or 0 if security is disabled; and (2) internal jumper '
    'J132 on the 03458-66505 (-66515 for option 001) assembly. With J132 in '
    'the RIGHT position neither CAL nor ACAL will run even with the correct '
    'security code, and clearing it means powering the meter down and '
    'removing the top cover to move J132 to the LEFT position (instrument '
    'front facing you). The 3458A has no rear-panel calibration switch. See '
    'Calibration Manual p.21-23.')


class CalibrationError(Exception):
    """An external adjustment (``CAL``) failed.

    :attr:`err` and :attr:`auxerr` hold the raw weighted sums read back from
    ``ERR?`` and ``AUXERR?``. Neither ever contains the security code.
    """

    def __init__(self, message: str, err: int = 0, auxerr: int = 0):
        self.err = int(err)
        self.auxerr = int(auxerr)
        super().__init__(message)


class CalibrationLockoutError(CalibrationError):
    """``CAL`` was refused because calibration access is locked out.

    The 3458A gates ``CAL`` two ways, and **neither can be queried remotely**
    — this exception is the only way a driver learns about either:

    1. the **security code** set with ``SECURE`` (Calibration Manual p.21).
       The factory value is documented inconsistently (the Calibration Manual
       says ``3458``, the User's Guide says ``3458A``) and a code of ``0``
       disables the feature. Pass it as ``security_code=``;
    2. the internal **jumper J132** on the 03458-66505 (or -66515 for option
       001) assembly (Calibration Manual p.23, "Hardware lock-out of
       calibration"). With J132 in the right position, *neither* ``CAL`` nor
       ``ACAL`` will execute **even with the correct security code**, and
       clearing it means powering the meter down and removing the top cover.

    This is deliberately *not* modelled as a rear-panel switch: the 3458A has
    no such switch. Its rear panel carries only GPIB, Ext Trig, Ext Out, the
    line-voltage selector and the ground lug. The instrument in this project
    that does have one is the Fluke 5700A — see
    ``drivers/fluke5700a.py``'s :class:`CalibrationSwitchError`.
    """

    def __init__(self, message: str, err: int = 0, auxerr: int = 0,
                 what: str = None):
        self.what = what or 'the CAL command'
        super().__init__(message, err, auxerr)


class CalibrationWriteNotAllowed(CalibrationError):
    """A calibration-memory write was attempted without the explicit opt-in.

    Raised *before* anything is transmitted.
    """


class HP3458A(MultimeterBase):
    _gpib_address: int
    _gpib_bus: int
    _nplc: int
    _azero: bool
    _ocomp: bool
    _freerunning: bool
    _max_input: float
    _delay: float
    _func: Func
    _display_en: bool

    _dev: Gpib.Gpib = None

    # Ceiling for waiting on a single conversion. SYNC-method ACV readings
    # with a low ACBAND edge can legitimately take minutes; callers may
    # raise this per-instance.
    read_timeout_sec: float = 60.0

    def __init__(self, gpib_address: int = 22, gpib_bus: int = 0, nplc: int = 10, name: str = None,
                 ocomp: bool = True, azero: bool = True, func: Func = Func.DCV, max_input: float = -1,
                 delay: float = 0.25, timeout: int = 13, freerunning: bool = False, display_en: bool = False,
                 allow_calibration_write: bool = False):
        super().__init__(name=name)
        # Opt-in gate for every method that writes calibration memory. Off by
        # default and never flipped by the driver itself; follows the
        # ``service=`` precedent in ``fluke5700a.py``. The security code is
        # deliberately *not* a constructor argument — it is never stored on
        # the instance, so it can never leak through a repr or a traceback.
        self.allow_calibration_write = bool(allow_calibration_write)
        self._gpib_address = gpib_address
        self._gpib_bus = gpib_bus
        self._nplc = nplc
        self._azero = azero
        self._ocomp = ocomp
        self._freerunning = freerunning
        self._delay = delay
        self.display_en = display_en

        self._dev = Gpib.Gpib(gpib_bus, gpib_address, timeout=timeout)

        self._dev_configure()

        self.set_display_enabled(display_en)
        self.set_func(func, max_input)

    @property
    def gpib_address(self) -> int:
        return self._gpib_address

    @property
    def max_input(self) -> float:
        return self._max_input

    @max_input.setter
    def max_input(self, max_input: float) -> None:
        self.set_func(self._func, max_input)

    @property
    def delay(self) -> float:
        return self._delay

    @delay.setter
    def delay(self, delay: float) -> None:
        self._delay = delay
        self._dev.write(f'DELAY {delay:.2E}')

    @property
    def nplc(self) -> int:
        return self._nplc

    @nplc.setter
    def nplc(self, nplc: int) -> None:
        with self._lock:
            self._nplc = nplc
            self._dev.write(f'NPLC {nplc}')

    # -- AZERO / OCOMP --
    #
    # These exist because ``_azero`` / ``_ocomp`` were previously settable only
    # at construction, which forced callers (notably ``cal/adapters.py``) to
    # send raw ``AZERO``/``OCOMP`` strings via :meth:`cmd`. That left the
    # cached attributes disagreeing with the wire state. Use these instead:
    # they are the only way to change either setting and keep the cache true.

    @property
    def autozero(self) -> bool:
        """Cached AZERO state. Truthful only if AZERO is changed *via*
        :meth:`set_autozero` and never by a raw ``cmd("AZERO ...")``."""
        return self._azero

    @autozero.setter
    def autozero(self, on: bool) -> None:
        self.set_autozero(on)

    def set_autozero(self, on: bool) -> None:
        """AZERO ON/OFF — periodic internal zero measurement.

        Keeps :attr:`autozero` in step with the instrument. Note this is
        neither ``ACAL`` nor ``CAL``: it changes nothing in calibration
        memory, it only controls whether each reading is offset-corrected.
        """
        with self._lock:
            self._azero = bool(on)
            self._dev.write(f'AZERO {self._bool_to_onoff(on)}')

    @property
    def ocomp(self) -> bool:
        """Cached OCOMP state. Truthful only if OCOMP is changed *via*
        :meth:`set_ocomp` and never by a raw ``cmd("OCOMP ...")``."""
        return self._ocomp

    @ocomp.setter
    def ocomp(self, on: bool) -> None:
        self.set_ocomp(on)

    def set_ocomp(self, on: bool) -> None:
        """OCOMP ON/OFF — offset-compensated ohms.

        Keeps :attr:`ocomp` in step with the instrument. The Calibration
        Manual (p.44) recommends OCOMP ON before adjusting against a
        resistance standard, so that thermal EMFs do not bias the adjustment.
        """
        with self._lock:
            self._ocomp = bool(on)
            self._dev.write(f'OCOMP {self._bool_to_onoff(on)}')

    @property
    def freerunning(self) -> bool:
        return self._freerunning

    @freerunning.setter
    def freerunning(self, en: bool) -> None:
        with self._lock:
            self._freerunning = en
            self._dev.write(f'TARM {"AUTO" if en else "HOLD"}')

    def start(self) -> None:
        self.freerunning = True

    def stop(self) -> None:
        self.freerunning = False

    def display_message(self, message: str) -> None:
        with self._lock:
            self._dev.write(f'DISP MSG,\"{message[0:75]}\"')

    def display_clear(self) -> None:
        with self._lock:
            self._dev.write('DISP CLR')

    def set_display_enabled(self, en: bool = True) -> None:
        if en:
            self.display_on()
        else:
            self.display_off()

    def display_off(self) -> None:
        with self._lock:
            self._display_en = False
            self._dev.write('DISP OFF,\"                 \"')

    def display_on(self) -> None:
        with self._lock:
            self._display_en = True
            self._dev.write('DISP ON')

    def set_func(self, func: Func, max_input: float = None) -> None:
        with self._lock:
            self._max_input = max_input
            self._func = func

            if max_input is not None and max_input > 0:
                self._dev.write(f'{func} {max_input:.2E}')
            else:
                self._dev.write(f'{func} AUTO')

    def set_range(self, max_input: float = -1) -> None:
        self.max_input = max_input

    def dcv(self, max_input: float = None) -> None:
        self.set_func(Func.DCV, max_input)

    def dci(self, max_input: float = None) -> None:
        self.set_func(Func.DCI, max_input)

    def ohm(self, max_input: float = None) -> None:
        self.set_func(Func.OHM, max_input)

    def ohmf(self, max_input: float = None) -> None:
        self.set_func(Func.OHMF, max_input)

    def set_ratio(self, ratio_en: bool = True) -> None:
        self.cmd(f'RATIO {"ON" if ratio_en else "OFF"}')

    # -- AC voltage (SETACV / ACBAND) --

    _VALID_ACV_METHODS = ('ANA', 'SYNC', 'RNDM')

    def set_acv_method(self, method: str = 'SYNC') -> None:
        """SETACV: select the AC measurement technique.

        ANA  = analog TRMS, 10 Hz-2 MHz, fastest;
        SYNC = synchronous sub-sampling, 1 Hz-10 MHz, best accuracy,
               requires a repetitive signal;
        RNDM = random sampling, 20 Hz-10 MHz, works on noise.
        Send before selecting the ACV function/range.
        """
        method = method.upper()
        if method not in self._VALID_ACV_METHODS:
            raise ValueError(f'invalid SETACV method: {method!r}')
        with self._lock:
            self._dev.write(f'SETACV {method}')

    def set_acband(self, low_hz: float, high_hz: float) -> None:
        """ACBAND: declare the signal band so the meter picks settling and
        sampling parameters. For a single tone set both edges near the tone
        (low edge governs measurement time: lower = slower)."""
        if low_hz <= 0 or high_hz < low_hz:
            raise ValueError(f'invalid ACBAND: {low_hz!r},{high_hz!r}')
        with self._lock:
            self._dev.write(f'ACBAND {low_hz:.6G},{high_hz:.6G}')

    def acv(self, max_input: float = None) -> None:
        self.set_func(Func.ACV, max_input)

    def set_lfilter(self, en: bool) -> None:
        """LFILTER: 75 kHz low-pass on the level-trigger path. Datasheet
        recommends ON for AC accuracy bands below ~50 kHz (prevents
        noise-induced false sync triggers)."""
        with self._lock:
            self._dev.write(f'LFILTER {self._bool_to_onoff(en)}')

    def set_sync_source(self, source: str = 'LEVEL', fallback: str = 'AUTO') -> None:
        """SSRC: sync source for SYNC-method ACV (and SSAC/SSDC).

        fallback AUTO lets the meter silently degrade to random sampling
        when it cannot sync; HOLD makes it error out instead — prefer HOLD
        when a silently-degraded reading would be mistaken for a good one.
        """
        source = source.upper()
        fallback = fallback.upper()
        if source not in ('LEVEL', 'EXT') or fallback not in ('AUTO', 'HOLD'):
            raise ValueError(f'invalid SSRC: {source!r},{fallback!r}')
        with self._lock:
            self._dev.write(f'SSRC {source},{fallback}')

    # -- Timing estimates --

    #: The two functions OCOMP applies to. "OCOMP ON" is accepted in any
    #: function but offset compensation is a resistance measurement technique
    #: (Programming Guide :238), so the cached flag alone does not mean the
    #: meter is spending the time.
    _OHMS_FUNCS = (Func.OHM, Func.OHMF)

    #: The ac functions, whose reading time is not set by NPLC. The data
    #: sheet's slowest ac reading rate is 0.025 readings/s for SYNC and RNDM
    #: (docs/3458a/3458a-specifications.md:275-277), i.e. 40 s per reading,
    #: and a 7.5-digit SYNC reading on the cal-verify path is allowed up to
    #: 32 s (cal/adapters.py, sync_resolution_pct).
    _AC_FUNCS = (Func.ACV, Func.ACDCV, Func.ACI, Func.ACDCI)
    _AC_SLOWEST_SAMPLE_SEC = 40.0

    def ocomp_active(self) -> bool:
        """True when offset compensation is actually costing time right now.

        Both halves matter: ``OCOMP ON`` left over from a resistance point
        does nothing on DCV, and a resistance point with ``OCOMP OFF`` reads
        at the plain integration rate. Truthful only as far as :attr:`ocomp`
        is -- i.e. when OCOMP was set through :meth:`set_ocomp` rather than a
        raw ``cmd("OCOMP ...")``.

        Both attributes are read defensively because ``_ocomp`` and ``_func``
        are class *annotations* with no defaults: an instance built with
        ``__new__`` and never configured has neither, and "nothing has been
        selected" is honestly "no offset compensation in force" rather than
        an AttributeError raised from a capability hook.
        """
        return (bool(getattr(self, '_ocomp', False))
                and getattr(self, '_func', None) in self._OHMS_FUNCS)

    def expected_sample_sec(self) -> float:
        """Wall-clock time one reading should take *in the present setup*.

        ``_expected_conversion_sec`` alone is the DCV number; multiply it by
        :data:`_OCOMP_TIME_FACTOR` when offset compensation is in force, or a
        19 Ohm point at NPLC 100 is budgeted 3.7 s for a 23 s reading.

        The programmed ``DELAY`` is added on top, because it is inserted
        between the trigger event and the first sample event (Programming
        Guide "Delayed first sample"). ``DELAY -1`` -- the automatic setting
        -- is deliberately counted as **zero** rather than guessed at: the
        meter computes it from function, range and ACBAND and never reports
        the value, so any number chosen here would be invented. Zero is the
        honest lower bound, and callers add their own multiplicative headroom
        on top of this estimate precisely so that an unreported automatic
        delay cannot turn into a spurious timeout.
        """
        aperture = _expected_conversion_sec(self._nplc)
        if self.ocomp_active():
            aperture *= _OCOMP_TIME_FACTOR
        if getattr(self, '_func', None) in self._AC_FUNCS:
            # The DCV table says nothing about ac: budget the data sheet's
            # slowest reading rate rather than the integration time.
            aperture = max(aperture, self._AC_SLOWEST_SAMPLE_SEC)
        return aperture + max(self._delay, 0.0)

    def _read_timeout_sec(self, expected: float) -> float:
        """How long to wait for one reading before calling it a timeout.

        :attr:`read_timeout_sec` is a flat 60 s ceiling that predates any
        estimate of what the configuration costs; on its own it is *shorter*
        than a legitimate reading at, say, OHMF/NPLC 200/OCOMP ON (44 s of
        conversion plus settling plus polling overhead). Take whichever of
        the two is larger, so raising ``read_timeout_sec`` per instance still
        does what it always did, and a slow configuration is never failed for
        taking the time the manual says it takes.
        """
        return max(self.read_timeout_sec, expected * 1.5 + 10.0)

    def read_single_sample(self) -> float:
        # Freerunning (TARM AUTO): readings stream into the output buffer
        # continuously; just read the next one. Bus is held only for the
        # short read transfer, not for the conversion.
        if self._freerunning:
            with self._lock:
                stb = self._dev.serial_poll()
                if not (stb & 0x80):
                    timeout = self._read_timeout_sec(self.expected_sample_sec())
                    deadline = time.monotonic() + timeout
                    while not (stb & 0x80):
                        if time.monotonic() > deadline:
                            raise TimeoutError(
                                f'3458A: no data after {timeout:.0f} s '
                                f'(STB=0x{stb:02x})')
                        time.sleep(0.05)
                        stb = self._dev.serial_poll()
                return float(self._dev.read().decode('ascii').strip())

        # Non-blocking single-shot: trigger, release the GPIB lock, sleep
        # through ~95% of the expected conversion (per programming guide
        # §7.A — INBUF ON makes TARM SGL,1 return as soon as the bus
        # transfer completes), then serial-poll bit 7 (data available).
        # The bus is free during the sleep, allowing co-resident GPIB
        # instruments to use it. STB? cannot be used here per guide §7.D
        # (output buffer would shadow it); use the low-level serial poll.
        with self._lock:
            stb = self._dev.serial_poll()
            if stb & 0x80:
                # Stale reading from a prior aborted call; consume it.
                self._dev.read()
            self._dev.write("TARM SGL,1")
        expected = self.expected_sample_sec()
        if expected > 0:
            time.sleep(expected * 0.95)
        timeout = self._read_timeout_sec(expected)
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                stb = self._dev.serial_poll()
            if stb & 0x80:
                break
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f'3458A: no data after {timeout:.0f} s '
                    f'(STB=0x{stb:02x})')
            time.sleep(0.05)
        with self._lock:
            return float(self._dev.read().decode('ascii').strip())

    # -- ACAL (HP3458A-specific) --

    def has_acal(self) -> bool:
        return True

    def acal(self, acal_type: ACAL = ACAL.ALL, nowait: bool = False) -> None:
        """ACAL — *internal* self-calibration against the meter's own references.

        This is **not** an adjustment against an external standard and it is
        **not** :meth:`adjust`. ACAL measures the 3458A's internal 7 V and
        40 kOhm references and recomputes gain/offset constants from them; it
        removes drift *relative to those references* but can never make the
        meter more accurate than they already are. It needs no external
        equipment and is normally run daily or after a 1 degC change.

        To move absolute accuracy you need an external traceable standard on
        the terminals and the ``CAL`` command — :meth:`adjust`.

        .. warning::
           **This cannot report a lockout.** Jumper J132 blocks ``ACAL`` as
           well as ``CAL`` (Calibration Manual p.23), and ``ACAL`` also takes
           the security code when autocal security is on. But this method
           reads no error register afterwards, so a refused ``ACAL`` returns
           normally and looks exactly like a successful one. It is left
           ungated because it writes no external-adjustment constants, but
           check ``ERR?``/:meth:`error_register` yourself if it matters —
           or compare :meth:`calnum` across the call when autocal is secured.
           Pre-existing behaviour, not introduced by the adjustment work.
        """
        with self._lock:
            self._dev.write(f'ACAL {acal_type}')
            if not nowait:
                time.sleep(acal_type.duration)

    def has_internal_temp(self) -> bool:
        return True

    def get_temp(self) -> float:
        with self._lock:
            self._dev.write("TEMP?")
            data = self._dev.read().decode("ascii").strip()
            return float(data)

    def get_cal_const(self, const_id: int, cal_item: CalItem = CalItem.Actual) -> str:
        with self._lock:
            self._dev.write(f'CAL? {const_id}, {int(cal_item)}')
            return self._dev.read().decode("ascii").strip()

    def calnum(self) -> int:
        """CALNUM? — how many times this meter has been calibrated.

        Incremented by ``CAL``, and also by ``ACAL`` when autocal is secured.
        Stored in cal-protected memory. Reading it before and after an
        adjustment is the cheapest independent evidence that calibration
        memory actually changed.
        """
        with self._lock:
            self._dev.write('CALNUM?')
            return int(self._dev.read().decode("ascii").strip())

    def cal_string(self) -> str:
        """CALSTR? — the stored calibration message (read side).

        Readable regardless of the security state.
        """
        with self._lock:
            self._dev.write('CALSTR?')
            return self._dev.read().decode("ascii").strip()

    def error_register(self) -> int:
        """ERR? — read **and clear** the error register (weighted sum)."""
        with self._lock:
            self._dev.write('ERR?')
            return int(float(self._dev.read().decode("ascii").strip()))

    def aux_error_register(self) -> int:
        """AUXERR? — read **and clear** the auxiliary error register."""
        with self._lock:
            self._dev.write('AUXERR?')
            return int(float(self._dev.read().decode("ascii").strip()))

    # -- External adjustment (writes calibration memory) --
    #
    # Nothing below is reachable unless the driver was constructed with
    # ``allow_calibration_write=True``. The gate is checked before any byte is
    # transmitted.
    #
    # This layer emits commands and verifies what it can. It deliberately does
    # **not** sequence a calibration: the Calibration Manual's mandatory order
    # (offset front, offset rear, DC gain, then resistance), the operator
    # prompts and the audit trail belong to ``caldmm.cal.procedure``.

    def _require_calibration_write(self, what: str) -> None:
        if not self.allow_calibration_write:
            raise CalibrationWriteNotAllowed(
                f'{what} writes the 3458A\'s non-volatile calibration memory '
                f'and cannot be undone. Construct the driver with '
                f'allow_calibration_write=True if you really mean to adjust '
                f'this meter; nothing was sent.')

    @staticmethod
    def _format_security_code(security_code) -> str:
        """Render the ``SECURE`` code for transmission without echoing it.

        The Calibration Manual (p.21) types it as an integer from -2.1E9 to
        2.1E9, rounded if fractional, with 0 meaning "security disabled". The
        factory value is documented inconsistently — ``3458`` in the
        Calibration Manual, ``3458A`` in the User's Guide — so a string is
        accepted rather than forced through ``int()``.

        Strings are still validated against :data:`_SECURITY_CODE_RE`. Passing
        one through verbatim would let a newline or a semicolon **append a
        second command to an irreversible write** — the one place in this
        driver where an injected command would land on the calibration path.
        The rejection message never echoes the offending value.
        """
        if isinstance(security_code, bool):
            raise ValueError('security code must be a number or a string')
        if isinstance(security_code, str):
            token = security_code.strip()
            if not _SECURITY_CODE_RE.match(token):
                raise ValueError(
                    'security code must be alphanumeric (optionally leading '
                    '"-"), with no whitespace, control characters, commas or '
                    'semicolons — anything else could inject a second command '
                    'onto a calibration write path. The code itself is '
                    'deliberately not echoed.')
            return token
        if isinstance(security_code, (int, float)):
            return str(int(security_code))
        raise ValueError('security code must be a number or a string')

    def adjust(self, value: float, security_code=None,
               wait: bool = True, wait_sec: float = None) -> int:
        """Adjust against an EXTERNAL standard — OVERWRITES CALIBRATION
        MEMORY, CANNOT BE UNDONE.

        This is the ``CAL`` command. It is **not** :meth:`acal`: ACAL retunes
        the meter against its own internal references and changes nothing
        about absolute accuracy, whereas this retrains those internal
        references against whatever standard is physically on the terminals.
        Getting the two confused is the classic 3458A mistake.

        Emits exactly one command::

            CAL <value>                 (no security code set)
            CAL <value>,<code>          (security code set)

        *value* is the **true value of the standard actually applied**, not
        its nominal value — if the 10 V reference really puts out 10.0001 V,
        pass 10.0001. Which adjustment runs is chosen by the magnitude, per
        the Calibration Manual:

        =============  ==============================  =========
        ``value``      adjustment                       duration
        =============  ==============================  =========
        ``0``          offset ("DC zeros"), needs a     ~5 min
                       four-wire low-thermal short;
                       run once per Terminals switch
                       position
        1 to 12 V      DC gain (10 V recommended)      ~2 min
        1k to 12k Ohm  resistance and DC current       ~12 min
                       (10 kOhm recommended)
        =============  ==============================  =========

        Anything outside those bands is refused locally, before transmission.
        The caller is responsible for selecting the matching function
        (DCV for a voltage standard, OHMF for a resistance standard) and, per
        the Calibration Manual p.44, for setting ``OCOMP ON`` — see
        :meth:`set_ocomp` — before adjusting against a resistance standard.

        Success requires *both* clean error registers **and** an advanced
        ``CALNUM?``. The counter is the only documented evidence that
        calibration memory actually changed (Calibration Manual p.23), and it
        is checked because the meter's error response to a *refused* ``CAL``
        is undocumented: a lockout that sets no error bit is otherwise
        indistinguishable from success. A refused ``CAL`` cannot advance the
        counter, so this turns an inference into a positive check.

        Returns the ``CALNUM?`` value read afterwards.

        *wait_sec* overrides how long this sleeps while the meter calibrates;
        it defaults to the documented duration for the point. It is a wait,
        not a timeout — there is no polling to abort, so shortening it just
        means reading the error registers before the meter has finished.

        The instrument must not be free-running: streaming readings would
        interleave with the error queries below. Call :meth:`stop` first
        rather than having this method silently change the trigger state.

        :raises CalibrationWriteNotAllowed: if the driver was not constructed
            with ``allow_calibration_write=True`` (nothing is transmitted).
        :raises CalibrationLockoutError: if access was refused — the security
            code, or internal jumper J132.
        :raises CalibrationError: if the adjustment itself failed.
        """
        self._require_calibration_write('CAL')
        kind = adjustment_kind(value)          # validates; raises before any I/O
        value = float(value)
        code = (None if security_code is None
                else self._format_security_code(security_code))

        if self._freerunning:
            raise ValueError(
                'refusing to adjust while the meter is free-running (TARM '
                'AUTO): streaming readings would be mistaken for the ERR? '
                'response. Call stop() first. Nothing was sent.')

        if wait_sec is None:
            wait_sec = expected_adjustment_seconds(value)

        # Clear any stale error state so whatever we read back afterwards is
        # unambiguously from this adjustment, and drain a leftover reading
        # from the output buffer for the same reason.
        self.error_register()
        self.aux_error_register()
        count_before = self.calnum()
        with self._lock:
            if self._dev.serial_poll() & 0x80:
                self._dev.read()
            # The security code is appended here and nowhere else; it is not
            # stored, logged or echoed.
            self._dev.write(f'CAL {value:.10G}' if code is None
                            else f'CAL {value:.10G},{code}')

        if wait:
            # INBUF ON (see _dev_configure) makes the write above return as
            # soon as the GPIB transfer completes, so the meter is still busy.
            time.sleep(wait_sec)

        err = self.error_register()
        auxerr = self.aux_error_register() if err & ERR_HARDWARE else 0
        self._raise_for_adjustment_errors(err, auxerr, kind=kind, value=value)

        count_after = self.calnum()
        if count_after == count_before:
            raise CalibrationLockoutError(
                f'CAL {value:.10G} ({kind}) set no error bits but CALNUM? did '
                f'not advance (still {count_after}), so nothing was written '
                f'to calibration memory. A silent refusal is exactly what a '
                f'hardware lockout looks like. {_LOCKOUT_ADVICE}',
                err, auxerr, what='CAL')
        return count_after

    def _raise_for_adjustment_errors(self, err: int, auxerr: int,
                                     kind: str, value: float) -> None:
        """Turn the post-``CAL`` error registers into an exception, or pass.

        **The mapping from "access refused" to a specific error bit is not
        documented.** The Calibration Manual says J132 blocks ``CAL`` but
        never says what the meter reports when it does, and it does not say
        what a wrong security code reports either. The most specific evidence
        available is AUXERR bit 14, "cal RAM write/protection failure", which
        is treated here as the lockout signal; every other calibration error
        still names the two possible lockouts in its message, because a
        procedure operator who is locked out needs to hear about the jumper
        whichever bit the meter happened to set.
        """
        if not (err or auxerr):
            return

        lockout_advice = _LOCKOUT_ADVICE
        detail = (f'CAL {value:.10G} ({kind}) reported '
                  f'ERR?={err} [{_decode_error_bits(err, _ERR_BIT_NAMES)}], '
                  f'AUXERR?={auxerr} '
                  f'[{_decode_error_bits(auxerr, _AUXERR_BIT_NAMES)}]')

        if auxerr & AUXERR_CAL_RAM_WRITE_PROTECTION:
            raise CalibrationLockoutError(
                f'{detail}. Calibration RAM is write-protected, so nothing '
                f'was stored. {lockout_advice}',
                err, auxerr, what='CAL')
        if auxerr & AUXERR_CAL_VALUE_OUT_OF_RANGE:
            raise CalibrationError(
                f'{detail}. The meter could not reconcile the value passed to '
                f'CAL with the signal on its terminals — check that the '
                f'standard is connected, settled, and on the function you '
                f'selected.', err, auxerr)
        if err & (ERR_CALIBRATION | ERR_HARDWARE):
            raise CalibrationError(
                f'{detail}. Treat the adjustment as FAILED and read CALNUM? '
                f'and the front panel before doing anything else. If the '
                f'meter refused outright rather than failing to converge: '
                f'{lockout_advice}', err, auxerr)
        raise CalibrationError(
            f'{detail}. Treating the adjustment as FAILED rather than '
            f'assuming it passed.', err, auxerr)

    def set_cal_string(self, message: str, security_code=None) -> None:
        """Store the calibration message — WRITES NON-VOLATILE MEMORY.

        Emits ``CALSTR '<message>'`` (plus the security code if one is set).
        Up to 75 characters are stored; longer messages are rejected rather
        than silently truncated.

        This is metadata, not an adjustment: it changes no measurement and
        does not advance ``CALNUM?``.
        """
        self._require_calibration_write('CALSTR')
        if not isinstance(message, str):
            raise ValueError(f'calibration message must be a string, got {message!r}')
        if len(message) > 75:
            raise ValueError(
                f'calibration message is {len(message)} chars; the 3458A '
                f'stores at most 75. Shorten it rather than let the meter '
                f'truncate it.')
        if "'" in message or '"' in message:
            raise ValueError('calibration message must not contain quotes')
        code = (None if security_code is None
                else self._format_security_code(security_code))

        self.error_register()
        with self._lock:
            self._dev.write(f"CALSTR '{message}'" if code is None
                            else f"CALSTR '{message}',{code}")
        err = self.error_register()
        auxerr = self.aux_error_register() if err & ERR_HARDWARE else 0
        if err or auxerr:
            raise CalibrationError(
                f'CALSTR reported '
                f'ERR?={err} [{_decode_error_bits(err, _ERR_BIT_NAMES)}], '
                f'AUXERR?={auxerr} '
                f'[{_decode_error_bits(auxerr, _AUXERR_BIT_NAMES)}]; the '
                f'message was probably not stored.', err, auxerr)

    def set_security_code(self, old_code, new_code, acal_secure: bool = None) -> None:
        """Change the calibration security code — WRITES NON-VOLATILE MEMORY.

        Emits ``SECURE <old>,<new>`` or ``SECURE <old>,<new>,<ON|OFF>``.
        Neither code appears in any echo, log or exception.

        Passing ``new_code=0`` **disables** calibration security altogether,
        so ``CAL`` and ``ACAL`` will then run with no code at all. That is a
        legitimate lab choice but it is not the default and it is not
        reversible without knowing the code you replaced.

        *acal_secure* controls whether ``ACAL`` also demands the code (factory
        setting: ON). Note the Calibration Manual documents the factory code
        as ``3458`` while the User's Guide says ``3458A``; check the meter
        rather than trusting either.
        """
        self._require_calibration_write('SECURE')
        old = self._format_security_code(old_code)
        new = self._format_security_code(new_code)

        self.error_register()
        command = f'SECURE {old},{new}'
        if acal_secure is not None:
            command = f'{command},{self._bool_to_onoff(acal_secure)}'
        with self._lock:
            self._dev.write(command)
        err = self.error_register()
        auxerr = self.aux_error_register() if err & ERR_HARDWARE else 0
        if err or auxerr:
            raise CalibrationError(
                f'SECURE reported '
                f'ERR?={err} [{_decode_error_bits(err, _ERR_BIT_NAMES)}], '
                f'AUXERR?={auxerr} '
                f'[{_decode_error_bits(auxerr, _AUXERR_BIT_NAMES)}]. The code '
                f'may or may not have changed — verify before relying on '
                f'either value. The codes are deliberately not echoed here.',
                err, auxerr)

    # -- Burst capture (3458A internal memory) --

    _VALID_MFORMAT = ('ASCII', 'SINT', 'DINT', 'SREAL', 'DREAL')
    _VALID_MEM_MODE = ('OFF', 'FIFO', 'LIFO', 'CONT')
    _VALID_NRDGS_SOURCE = ('AUTO', 'EXT', 'SGL', 'HOLD', 'TIMER', 'SYN', 'LINE')

    # Conservative ASCII-mode RAM budget. The default-RAM 3458A holds
    # ~580 ASCII readings at NDIG 9; with NDIG reduced you get more,
    # but the floor is set by the worst case. burst_capture() asserts
    # this and the host-side caller chunks larger requests.
    _BURST_ASCII_LIMIT = 500

    def set_mformat(self, fmt: str = 'ASCII') -> None:
        """MFORMAT: storage format used by internal MEM.

        ASCII (default) is human-readable but the bulkiest (~16 B/sample).
        SINT/DINT/SREAL/DREAL are packed binary; not parsed by the
        current driver. Use ASCII unless you have a binary parser ready.
        """
        fmt = fmt.upper()
        if fmt not in self._VALID_MFORMAT:
            raise ValueError(f'invalid MFORMAT: {fmt!r}')
        with self._lock:
            self._dev.write(f'MFORMAT {fmt}')

    def set_mem(self, mode: str = 'OFF') -> None:
        """MEM: enable/disable internal-RAM storage.

        Switching modes (including OFF→FIFO) erases stored readings.
        """
        mode = mode.upper()
        if mode not in self._VALID_MEM_MODE:
            raise ValueError(f'invalid MEM mode: {mode!r}')
        with self._lock:
            self._dev.write(f'MEM {mode}')

    def set_nrdgs(self, n: int, source: str = 'AUTO') -> None:
        """NRDGS N, source — readings per trigger and per-reading sample event."""
        source = source.upper()
        if source not in self._VALID_NRDGS_SOURCE:
            raise ValueError(f'invalid NRDGS source: {source!r}')
        if n <= 0:
            raise ValueError(f'NRDGS count must be >= 1, got {n}')
        with self._lock:
            self._dev.write(f'NRDGS {int(n)},{source}')

    def mcount(self) -> int:
        """MCOUNT? — number of readings currently stored in internal MEM.

        Some firmware revisions on the 3458A leak the first sample of a
        burst into the output buffer despite MEM FIFO being set. If the
        first response to MCOUNT? parses as a float (it's a leaked
        reading), we discard it and re-query up to 5 times.
        """
        with self._lock:
            for _ in range(5):
                self._dev.write('MCOUNT?')
                raw = self._dev.read().decode('ascii').strip()
                try:
                    return int(raw)
                except ValueError:
                    # Try as float — sanity check that it really is a leaked
                    # reading and not random garbage.
                    try:
                        float(raw)
                    except ValueError:
                        raise ValueError(f'MCOUNT? returned unparseable: {raw!r}')
                    # It's a leaked sample reading; discard and re-query.
                    continue
            raise RuntimeError(
                '3458A: MCOUNT? kept returning sample readings instead of '
                'a count (output-buffer leak; tried 5 times)')

    def recall_mem(self, start: int = 1, n: int = 1) -> list[float]:
        """RMEM start,n,1 — recall N samples from internal MEM as floats.

        Only valid for ASCII MFORMAT. The 3458A streams one reading per
        EOI-terminated line; some firmware revisions also emit commas
        within a chunk, so both delimiters are accepted.
        """
        if start < 1 or n < 1:
            raise ValueError(f'invalid RMEM range: start={start}, n={n}')
        out: list[float] = []
        with self._lock:
            self._dev.write(f'RMEM {int(start)},{int(n)},1')
            deadline = time.monotonic() + max(60.0, n * 0.05)
            while len(out) < n:
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f'3458A: RMEM returned {len(out)} of {n} (timed out)')
                try:
                    raw = self._dev.read().decode('ascii')
                except Exception:
                    time.sleep(0.05)
                    continue
                for tok in raw.replace(',', '\n').replace('\r', '\n').split('\n'):
                    tok = tok.strip()
                    if not tok:
                        continue
                    try:
                        out.append(float(tok))
                    except ValueError:
                        pass
                    if len(out) >= n:
                        break
        return out

    #: Why :meth:`burst_capture` refuses outright when offset compensation is
    #: in force. :meth:`has_burst` now withdraws the burst path for *every*
    #: resistance measurement, which subsumes this case; the hard refusal
    #: stays scoped to the combination that was watched not to return, since
    #: that is the one a direct caller cannot wait out.
    _OCOMP_BURST_REASON = (
        'the 3458A does not capture offset-compensated ohms into MEM: with '
        'OHMF/NPLC 100/AZERO ON/OCOMP ON and NRDGS 2,AUTO + MEM FIFO + '
        'TARM SGL, MCOUNT? stayed at 0 while exactly one reading leaked to '
        'the output buffer, and it was still 0 after 180 s -- roughly 8x the '
        '23.2 s a single reading of the same point takes (measured). '
        'read_single_sample() reads the same point correctly, '
        'so read_samples() loops over it instead')

    def has_burst(self) -> bool:
        """True except while a resistance or ac function is selected.

        ``MultimeterBase.read_samples`` routes every request through
        :meth:`burst_capture` when this is True, which put a 4-wire
        resistance verification straight into a path the instrument does not
        implement *at the integration time a resistance verification uses*.
        Sourcing a real 190 kOhm from a 5700A, 4-wire, ``OCOMP OFF``,
        ``AZERO ON`` -- the 1 MOhm range, where the 3458A offers no offset
        compensation at all (Programming Guide :238, "10 Ohm - 100 kOhm
        ranges only") -- gave (measured):

        =========  ==========  ===========================================
        NPLC 10    burst of 2  OK, 1.50 s, two good values
        NPLC 10    burst of 4  OK, 3.61 s, four good values
        NPLC 100   burst of 2  0 in MEM + 1 leaked, after 120.0 s
        NPLC 100   single      :meth:`read_single_sample` OK, 4.07 / 4.64 s
        =========  ==========  ===========================================

        against a completed DCV run at the same NPLC 100 in bursts of five
        (15/15
        points). So it is neither the function alone nor the integration
        time alone but the two together: **ohms at high NPLC never puts its
        readings in MEM**, while DCV at the same NPLC and ohms at a tenth of
        it both burst normally.

        The rule is written on the function rather than on NPLC because the
        boundary is somewhere in (10, 100] and no number in that interval
        has been measured; picking one would be inventing a firmware
        threshold, and ``cal/data/hp3458a.yaml`` pins RES to ``nplc: 100``
        for accuracy, so in practice ohms is always at the failing setting.
        Nothing proven to work is destroyed: only the *automatic* choice of
        the path is withdrawn, and a caller that deliberately wants a fast
        low-NPLC ohms burst can still call :meth:`burst_capture` itself.

        Waiting longer does not help -- the readings never arrive in MEM --
        so this is a routing decision, not a timeout budget. DCV and dc
        current burst exactly as before.

        Whether the underlying firmware behaviour has a documented cause is
        unknown. The programming guide's MEM/NRDGS chapter (:399-:430,
        :890-:900) says nothing about function or integration time, and its
        one function-dependent burst rule -- "high-speed mode", entered for
        DCV/DCI/OHM/OHMF at *<=* 10 PLC with SINT/DINT formats (:573-:579)
        -- is neither the configuration used here (ASCII, NPLC 100) nor the
        right direction, since it would describe the case that works.
        Confirming a mechanism would need the bench; declining the path is
        the part that can be verified without hardware.
        """
        if getattr(self, '_func', None) in self._OHMS_FUNCS:
            return False
        # The ac functions read one at a time as well: the only ac path proven
        # is read_single_sample, and a SYNC burst of 2 at the DCV-derived
        # 12.6 s budget timed out with 0 in MEM.
        # Whether a burst would
        # complete given 40 s per reading is untested.
        if getattr(self, '_func', None) in self._AC_FUNCS:
            return False
        # DCI fails exactly as ohms does (measured, input
        # open, OCOMP OFF): burst of 2 OK at NPLC 10, "0 in MEM + 1 leaked"
        # at NPLC 100, while DCV bursts at both; read_single_sample gives DCI
        # NPLC 100 in 4.2 s. So DCV is the only function left on the path.
        if getattr(self, '_func', None) is Func.DCI:
            return False
        # Subsumed by the test above while that one is function-wide, and
        # kept anyway: it is true for its own reason (a separate bench
        # observation, and the one burst_capture itself refuses on), so
        # narrowing the ohms rule later must not silently take
        # offset-compensated ohms back into the burst path with it.
        return not self.ocomp_active()

    def burst_capture(self, n: int, *, timeout_sec: float = None) -> list[float]:
        """Capture N consecutive readings into internal RAM and return them.

        Sets MEM FIFO + MFORMAT ASCII + NRDGS n,AUTO, fires TARM SGL,
        polls MCOUNT until the burst completes (or timeout_sec elapses),
        then recalls the readings via RMEM and restores the steady-state
        (MEM OFF, NRDGS 1,AUTO, and the prior TARM mode).

        Cap: n <= 500 per call to stay within the default-RAM 3458A's
        ASCII-mode budget. Callers needing more samples should chunk.

        The instrument uses its current NPLC and DELAY for the per-sample
        cadence, so noise characterization should configure those first.

        Refuses offset-compensated ohms outright. The refusal is here as well
        as in the capability hook because ``burst_capture`` is also called
        directly (``apps/dmm_bias.py``, ``tools/dmm_noise.py``), and a caller
        that skipped the hook would otherwise spend the whole timeout
        discovering the same thing.

        It is deliberately *narrower* than :meth:`has_burst`, which withdraws
        the path for resistance generally: ohms bursts do work at NPLC 10
        (measured), so a caller that asked for one by
        name gets it rather than a refusal for a configuration nobody has
        seen fail.
        """
        if n <= 0:
            raise ValueError(f'n must be >= 1, got {n}')
        if n > self._BURST_ASCII_LIMIT:
            raise ValueError(
                f'n={n} exceeds per-burst ASCII RAM budget '
                f'({self._BURST_ASCII_LIMIT}); chunk on the host')
        if self.ocomp_active():
            raise RuntimeError(f'3458A burst refused: {self._OCOMP_BURST_REASON}')

        # Per *reading*, and DELAY is really inserted once per trigger group
        # rather than once per reading, so n * aperture over-counts a
        # delayed burst by (n-1) delays. That is the safe direction for a
        # timeout, and the pre-sleep below is capped at 90 % of it.
        aperture = self.expected_sample_sec()
        if timeout_sec is None:
            timeout_sec = n * aperture * 1.5 + 10.0

        original_freerunning = self._freerunning
        try:
            with self._lock:
                self._dev.write('TARM HOLD')
                # Drain any stale reading left in the output buffer by a
                # prior configuration change.
                stb = self._dev.serial_poll()
                if stb & 0x80:
                    self._dev.read()
                self._dev.write('MFORMAT ASCII')
                self._dev.write('MEM FIFO')
                self._dev.write(f'NRDGS {int(n)},AUTO')
                self._dev.write('TARM SGL')

            # Sleep most of the expected duration before polling so we
            # don't hammer MCOUNT? while the meter is busy.
            expected = n * aperture
            time.sleep(min(expected * 0.9, max(timeout_sec - 1.0, 0.0)))

            # Some firmware revisions leak the first reading(s) of a burst
            # into the output buffer despite MEM FIFO being set. Collect
            # any such leaks so we can prepend them to the final result;
            # also reduces the count we expect from MEM.
            leaked: list[float] = []

            def _drain_leaks() -> None:
                with self._lock:
                    for _ in range(5):
                        stb = self._dev.serial_poll()
                        if not (stb & 0x80):
                            return
                        raw = self._dev.read().decode('ascii').strip()
                        try:
                            leaked.append(float(raw))
                        except ValueError:
                            pass

            deadline = time.monotonic() + timeout_sec
            while True:
                _drain_leaks()
                count = self.mcount()
                needed_in_mem = n - len(leaked)
                if count >= needed_in_mem:
                    break
                if time.monotonic() > deadline:
                    raise TimeoutError(
                        f'3458A burst: {count} in MEM + {len(leaked)} leaked '
                        f'!= {n} requested after {timeout_sec:.1f} s')
                time.sleep(max(aperture * 0.5, 0.2))

            # Final fetch: leaked samples are chronologically first,
            # MEM holds the remainder in FIFO order.
            mem_n = n - len(leaked)
            if mem_n > 0:
                return leaked + self.recall_mem(start=1, n=mem_n)
            return leaked[:n]
        finally:
            with self._lock:
                self._dev.write('NRDGS 1,AUTO')
                self._dev.write('MEM OFF')
                self._dev.write(f'TARM {"AUTO" if original_freerunning else "HOLD"}')
            self._freerunning = original_freerunning

    def cmd(self, cmd_str: str) -> None:
        with self._lock:
            self._dev.write(cmd_str)

    def query(self, cmd_str: str) -> str:
        with self._lock:
            self._dev.write(cmd_str)
            res = self._dev.read().decode("ascii").strip()

        return res

    def read_raw_str(self) -> str:
        with self._lock:
            return self._dev.read().decode("ascii").strip()

    @classmethod
    def _bool_to_onoff(cls, value: bool) -> str:
        return 'ON' if value else 'OFF'

    def _dev_configure(self) -> None:
        with self._lock:
            self._dev.clear()

            self._dev.write("PRESET NORM")
            # INBUF ON buffers commands so writes return as soon as the
            # GPIB transfer completes (instead of holding the bus until
            # the meter has executed the command). This is the prerequisite
            # for the non-blocking read_single_sample() pattern below —
            # without it, a slow command (e.g. ACAL) would lock the bus.
            self._dev.write("INBUF ON")
            self._dev.write("END ALWAYS")
            self._dev.write(f'AZERO {self._bool_to_onoff(self._azero)}')
            self._dev.write(f'OCOMP {self._bool_to_onoff(self._ocomp)}')
            self._dev.write(f'NPLC {self._nplc}')
            self._dev.write("OFORMAT ASCII")
            self._dev.write(f'DELAY {self._delay:.2E}')
            self._dev.write(f'TARM {"AUTO" if self._freerunning else "HOLD"}')
            self._dev.write("TRIG AUTO")
            self._dev.write("MEM OFF")
            self._dev.write("NDIG 9")
            self._dev.write("QFORMAT NUM")


# Backward-compatible alias
Worker = WorkerBase

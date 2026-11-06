"""Concrete roles: the only module in ``cal`` that is allowed to see a driver.

Everything else in :mod:`caldmm.cal` works against the Protocols in
:mod:`caldmm.cal.roles`.  This module is where a real Fluke 5700A, HP 3458A,
HP 34401A or Wavetek 1281 is bolted on, and it is deliberately the *only* place
where ``caldmm.drivers`` appears.

**Every driver import is lazy** — inside a function or a method, never at module
scope.  ``import caldmm.cal.adapters`` must succeed with linux-gpib, pyserial
and PyVISA all absent, so that the execution engine, the plan generator and the
report writer stay importable on a laptop with no lab attached.  Importing this
module pulls in ``specs`` and ``roles`` and nothing else.

What an adapter absorbs
-----------------------

An ABC cannot express any of this, which is why it lives here rather than in
``MultimeterBase``:

* the 3458A needs ``SETACV`` and ``ACBAND`` set **before** the function is
  selected, distinguishes ``OHM`` from ``OHMF``, and has no setter at all for
  ``AZERO``/``OCOMP`` — they are constructor arguments, so the adapter sends
  the commands;
* the 34401A chooses an ac filter from the frequency;
* ``Wavetek1281.set_func`` ignores its argument entirely — the instrument is
  dc-volts-only — so :meth:`Wavetek1281Meter.supports` has to say so, or the
  plan generator emits ohms points that quietly return dc volts;
* the invalid-reading sentinel differs per model, and a sentinel sample must be
  **counted and excluded**, never averaged.

Function names come from the YAML
---------------------------------

``FunctionSpec.func`` is the *name* of the driver's own ``Func`` member and
``DriverRef.func_enum`` names the enum; :func:`resolve_func` puts them together.
There is deliberately no ``Quantity -> Func`` table in this module: that
indirection is what lets a new meter be a YAML file plus a thin adapter.  Two
models have no ``Func`` enum at all (the 1281 and the 5700A) and their YAML
declares no ``func_enum``, so the resolver returns the bare name for them.

Manual ranging
--------------

Every adapter here fixes the range and turns autoranging **off** explicitly,
and records both in :attr:`~caldmm.cal.roles.MeterConfig.applied`
(``autorange: False``, ``range: <the argument sent>``).  Autoranging picks the
range from the signal, and the range is what decides the ppm-of-range term,
the resolution, the input impedance and — on ohms — the test current: a
reading taken on a range nobody recorded cannot be judged at all.  The 34401A
service guide states the same requirement for its own verification
(``34401a-service.txt`` 1285-1294, PDF p.31: "It is also assumed that manual
ranging is enabled to ensure proper full scale range selection").

Two things follow.  :meth:`MeterAdapter._set_function` **refuses** a ``None``
range rather than passing the drivers' shared ``-1`` autorange spelling, and
:meth:`MeterAdapter.range_readback` asks the instrument which range it is
actually on, so the engine can check before energizing.  The 1281 answers
``None`` there: its command set has no range query.

What ``applied`` carries
------------------------

More than the model file's ``apply:`` knobs.  It is the configuration record
the result row is built from, so it also carries what the adapter decided by
itself and what the datasheet's own conditions depend on: ``autorange`` and
``range`` above, the 3458A's ``resolution_pct`` / ``resolution_digits``,
``lfilter`` and ``sync_source``, its ``math_null`` / ``range_held_s`` (the
dc figures assume MATH NULL on a range held over four minutes), the 34401A's
``trigger_delay``, and a note wherever a knob the model file asked for is not
available on the selected range (``ocomp_note``).  A knob that could not be
applied is never recorded as applied; it is an :class:`AdapterError`.

Ambient temperature
-------------------

:func:`caldmm.cal.verdict.evaluate` will not return ``PASS`` when a model
declares a temperature window and no ambient was recorded.
:class:`StaticEnvironment` implements :class:`~caldmm.cal.roles.EnvironmentProbe`
so the runner has something to hand the engine; any other probe (a room
sensor, a logger) only has to provide the same one ``read()`` method.
"""

from __future__ import annotations

import importlib
import math
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence

from .plan import needs_range_lock, range_lock_anchor
from .quantities import Connection, Quantity
from .roles import (
    Environment,
    MeterConfig,
    Reading,
    SourceHealth,
    SourceState,
    Uncertainty,
)
from .specs import (
    APPLY_KNOBS,
    FunctionSpec,
    ModelSpec,
    RangeSpec,
    Role,
    load_model,
    select_band,
    select_mode,
    select_range,
)

__all__ = [
    "AdapterError",
    "CURRENT_POSTS",
    "parse_instrument_spec",
    "func_name",
    "resolve_func",
    "MeterAdapter",
    "HP3458AMeter",
    "Fluke5700ASource",
    "StaticEnvironment",
    "ADAPTERS",
    "adapter_class",
    "open_meter",
    "open_source",
]


#: Integration time for a wiring proof or pre-step (:meth:`MeterAdapter.\
#: configure_probe`).  10 PLC still resolves every shipped proof level to far
#: inside its +/-10 % window and costs a fraction of a second a reading with
#: autozero instead of several seconds at 100 PLC.
PROBE_MAX_NPLC = 10.0


class AdapterError(RuntimeError):
    """A role cannot carry out what it was asked to do."""


# --------------------------------------------------------------------------
# Small shared helpers
# --------------------------------------------------------------------------

def parse_instrument_spec(spec: str) -> tuple[str, str]:
    """Split the project's ``name:address`` instrument spelling.

    The *name* is an operator-chosen designator.  It says nothing about the
    model, the channel or the role, so the model id is always passed
    separately — see :func:`open_meter`.
    """
    if ':' in spec:
        name, addr = spec.split(':', 1)
        return name, addr
    return spec, spec


def parse_gpib_address(address: str) -> tuple[int, int]:
    """Split a GPIB address spelling into ``(bus, pad)``.

    Three spellings: a bare primary address (``"22"``, bus 0), ``"bus,pad"``
    (``"1,22"``) and ``"gpib<bus>,pad"`` (``"gpib1,22"``) for a bench where
    the instruments sit on different USB-GPIB adapters — the bus is the
    linux-gpib board index (``/dev/gpib0``, ``/dev/gpib1``, ...). The
    separator is a comma, **not** a colon: colons already delimit the
    ``model:name:address`` spelling upstream (:func:`parse_instrument_spec`,
    ``cal_verify``'s ``parse_role_spec``), and a colon-form bus would make
    ``fluke5700a:1:4`` ambiguous between a unit named ``1`` and a bus. The
    full operator spelling is ``5700a:gpib1,4``.

    The primary address is checked against 0-30: 31 is the untalk/unlisten
    address and nothing answers there, and a malformed address must never
    quietly become a different instrument.
    """
    text = str(address).strip().lower()
    bus_text, sep, pad_text = text.partition(',')
    if not sep:
        bus_text, pad_text = "0", text
    elif bus_text.startswith("gpib"):
        bus_text = bus_text[len("gpib"):]
    try:
        bus, pad = int(bus_text), int(pad_text)
    except ValueError:
        raise AdapterError(
            f"GPIB address {address!r} is not 'pad', 'bus,pad' or "
            f"'gpib<bus>,pad' (e.g. '22', '1,22', 'gpib1,22')") from None
    if not 0 <= pad <= 30:
        raise AdapterError(
            f"GPIB primary address {pad} is outside 0-30 (in {address!r})")
    if bus < 0:
        raise AdapterError(
            f"GPIB bus (linux-gpib board index) must be >= 0, got {bus} "
            f"(in {address!r})")
    return bus, pad


# Baud rates the 5700A's serial port can be set to ([OM Table 5-2, p.5-38],
# the ``SP_SET`` parameter list), and the factory default.  Mirrored from
# ``drivers/fluke5700a.py`` rather than imported: this module may only touch
# ``caldmm.drivers`` lazily inside a function, and address parsing happens
# well before any I/O.  ``tests/cal/test_adapters.py`` pins the two copies
# equal.
SERIAL_BAUD_RATES = (110, 300, 600, 1200, 2400, 4800, 9600, 19200)
DEFAULT_SERIAL_BAUD = 9600

#: Which binding posts the 5700A's current output leaves by — the ``CUR_POST``
#: parameter list ([OM 1996 Table 5-2, p.176]): ``NORMAL`` is the OUTPUT HI
#: post (**the factory default**), ``AUX`` the AUX CURRENT OUTPUT post,
#: ``IB5725`` the 5725A's.  The setting is nonvolatile and ``*RST`` does not
#: touch it, so a current group that neither reads nor sets it is cabled by
#: guesswork.  Mirrored from ``drivers/fluke5700a.py`` rather than imported,
#: for the same reason as :data:`SERIAL_BAUD_RATES`; ``tests/cal/
#: test_adapters.py`` pins the two copies equal.
CURRENT_POSTS = ("NORMAL", "AUX", "IB5725")


def _now() -> float:
    """Monotonic seconds.  Indirected so a test can hold time still."""
    return time.monotonic()


#: ``COM3`` or ``COM3@19200`` (case-insensitive): a Windows serial port.
_WINDOWS_COM_PORT = re.compile(r"^COM\d+(@\d+)?$", re.IGNORECASE)


def is_serial_address(address: str) -> bool:
    """True when an address names a serial device rather than a GPIB one.

    A device path (``/dev/ttyUSB0``) or a Windows port name (``COM3``,
    ``COM3@19200``) is a port; ``gpib1,4`` is a bus and primary address.  No
    GPIB spelling contains a slash or starts with ``COM`` followed by a digit,
    so the grammars cannot collide.
    """
    text = str(address).strip()
    return "/" in text or _WINDOWS_COM_PORT.match(text) is not None


def parse_serial_address(address: str) -> tuple[str, int]:
    """Split a serial address into ``(port, baud)``.

    Two spellings: a bare port (``/dev/ttyUSB0`` or ``COM3``, at the
    factory-default 9600 baud) and ``port@baud`` (``/dev/ttyUSB0@19200``,
    ``COM3@19200``) for a port that has
    been set faster.  The baud is checked against the rates the calibrator can
    actually be set to, because a rate it does not have produces no error at
    either end — just a link that never answers.
    """
    text = str(address).strip()
    port, sep, baud_text = text.rpartition("@")
    if not sep:
        return text, DEFAULT_SERIAL_BAUD
    try:
        baud = int(baud_text)
    except ValueError:
        raise AdapterError(
            f"serial address {address!r} is not 'port' or 'port@baud' "
            f"(e.g. '/dev/ttyUSB0', '/dev/ttyUSB0@19200', 'COM3@19200')") from None
    if baud not in SERIAL_BAUD_RATES:
        raise AdapterError(
            f"{baud} baud (in {address!r}) is not one of the calibrator's "
            f"rates: {', '.join(str(rate) for rate in SERIAL_BAUD_RATES)}")
    return port, baud


def _attr(point, name: str, default=None):
    """Read an optional attribute off a :class:`~caldmm.cal.roles.PointLike`."""
    value = getattr(point, name, default)
    return default if value is None else value


def func_name(spec: ModelSpec, quantity, connection=Connection.TWO_WIRE) -> str | None:
    """The driver's own function *name* for this quantity and wiring. **No I/O.**

    The connection's own ``func`` wins over the function's: the 3458A's
    resistance is ``OHMF`` in 4-wire and ``OHM`` in 2-wire, and that difference
    is data in the YAML, not a rule in code.
    """
    quantity = Quantity(quantity)
    connection = Connection(connection)
    function = spec.function(quantity)
    if function is None:
        return None
    conn_spec = function.connections.get(connection)
    if conn_spec is not None and conn_spec.func:
        return conn_spec.func
    return function.func


def resolve_func(spec: ModelSpec, quantity, connection=Connection.TWO_WIRE):
    """Resolve a quantity to the member of the driver's ``Func`` enum.

    Returns the enum member when the model declares a ``func_enum``, the bare
    name when it does not (the 1281 and the 5700A have no ``Func`` type at all),
    and ``None`` when the model does not name a function for this quantity.

    Imports the driver module — lazily, and only when actually called.
    """
    name = func_name(spec, quantity, connection)
    if name is None:
        return None
    driver = spec.driver
    if driver is None or not driver.func_enum:
        return name
    module = importlib.import_module(driver.module)
    enum = getattr(module, driver.func_enum, None)
    if enum is None:
        raise AdapterError(
            f"{spec.model_id}: {driver.module} has no {driver.func_enum!r} "
            f"enum, but the model file names one")
    try:
        return enum[name]
    except KeyError as exc:
        raise AdapterError(
            f"{spec.model_id}: {driver.func_enum}.{name} does not exist; the "
            f"model file's func: must name a member of the driver's enum"
        ) from exc


def _merged_knobs(function: FunctionSpec, range_spec: RangeSpec, mode,
                  point) -> dict:
    """The model's ``apply:`` knobs for this point, most specific last.

    Function, then range, then mode, then the point itself.  The range sits
    between function and mode because that is how the file nests: a mode
    belongs to a band, which belongs to a range.  ``specs.evaluate_limit``
    merges the same three in the same order, so the ``apply`` recorded beside
    a limit is the configuration that was really sent.
    """
    knobs = dict(function.apply)
    knobs.update(range_spec.apply)
    if mode is not None:
        knobs.update(mode.apply)
    knobs.update(dict(_attr(point, "apply", {}) or {}))
    return knobs


def _knob_order(knobs: Mapping[str, Any]) -> list[str]:
    """Knob names in the schema's declaration order, then anything unknown.

    Deterministic so that the emitted command sequence is reproducible and can
    be asserted; ``APPLY_KNOBS`` is a closed vocabulary, so the tail is
    normally empty.
    """
    known = [name for name in APPLY_KNOBS if name in knobs]
    return known + sorted(set(knobs) - set(known))


def _func_label(func) -> str | None:
    """How a resolved function is recorded: the enum member's name, or the name."""
    if func is None:
        return None
    return str(getattr(func, "name", func))


def _as_bool(value) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "on", "true", "yes")
    return bool(value)


def _band_edges(value, frequency_hz: float) -> tuple[float, float]:
    """Interpret an ``ac_band`` knob, defaulting to the tone itself.

    Never returns an unusable band.  Skipping ``ACBAND`` because the edges came
    out zero would leave the *previous* point's band in force with nothing
    recorded to say so, and on the 3458A the band selects both the settling and
    which accuracy table applies.
    """
    if value is None:
        parts = [frequency_hz, frequency_hz]
    elif isinstance(value, str):
        parts = [p for p in value.replace(",", " ").split() if p]
    else:
        parts = list(value)
    if len(parts) != 2:
        raise AdapterError(f"ac_band must be two frequencies, got {value!r}")
    low, high = float(parts[0]), float(parts[1])
    if not (low > 0.0) or not (high >= low) or not math.isfinite(high):
        raise AdapterError(
            f"ac_band must be 0 < low <= high, got ({low:g}, {high:g}); an ac "
            f"point with no usable band would silently keep the previous one")
    return (low, high)


# --------------------------------------------------------------------------
# Meters
# --------------------------------------------------------------------------

class MeterAdapter:
    """Base :class:`~caldmm.cal.roles.MeterRole` over a ``MultimeterBase``.

    Subclasses supply :attr:`MODEL_ID`, whatever knob handling their driver
    needs, and an :meth:`open` that knows the shape of their address.
    """

    #: Spec file / registry key.  Also the default ``model_id``.
    MODEL_ID: str = ""
    #: Quantities the *instrument* can do, when that is narrower than the file
    #: says.  ``None`` means "trust the specification".
    _QUANTITIES: frozenset | None = None
    #: True on every adapter in this module: each one selects a fixed range
    #: and turns autoranging off.  The engine's 10 %-pre-step fallback exists
    #: for a meter that cannot, so this is a capability rather than a rule.
    supports_manual_range: bool = True

    def __init__(self, device, *, spec: ModelSpec | None = None,
                 model_id: str | None = None):
        self._dev = device
        self._model_id = (model_id or self.MODEL_ID
                          or getattr(device, "model_id", None)
                          or type(device).__name__.lower())
        self._spec = spec if spec is not None else load_model(self._model_id)
        self._config: MeterConfig | None = None
        self._quantity: Quantity | None = None
        # What the last configure() selected, for range_readback() and for
        # the knobs whose availability depends on the range.
        self._function: FunctionSpec | None = None
        self._range_spec: RangeSpec | None = None
        self._func: Any = None

    # -- identity ---------------------------------------------------------

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def spec(self) -> ModelSpec:
        return self._spec

    @property
    def device(self):
        return self._dev

    @property
    def config(self) -> MeterConfig | None:
        """The last configuration applied, or ``None`` before the first one."""
        return self._config

    def __repr__(self) -> str:                 # pragma: no cover - debugging aid
        name = getattr(self._dev, "name", None)
        return (f"<{type(self).__name__} {self._model_id}"
                + (f" {name!r}" if name else "") + ">")

    # -- capability -------------------------------------------------------

    def supports(self, quantity, connection=Connection.TWO_WIRE) -> bool:
        """True when the specification declares this quantity and wiring.

        Capability is the specification — ``model.functions`` and their
        ``connections`` — and there is deliberately no second capability table
        to keep in step with it.
        """
        try:
            quantity = Quantity(quantity)
            connection = Connection(connection)
        except ValueError:
            return False
        if self._QUANTITIES is not None and quantity not in self._QUANTITIES:
            return False
        function = self._spec.function(quantity)
        if function is None:
            return False
        if not function.connections:
            return connection is Connection.TWO_WIRE
        return connection in function.connections

    # -- configuration ----------------------------------------------------

    def configure_probe(self, point) -> MeterConfig:
        """:meth:`configure` for a wiring proof or a 10 % pre-step.

        Same function and range, but read fast: a probe is judged to +/-10 %
        and never enters a verdict, so the plan's 100 PLC (3.4 s a reading on
        a 34401A with autozero) or its slow ac filter (7 s) buys nothing.
        :meth:`_probe_knobs` says what changes.  The engine always
        re-configures with the plan's own knobs, and discards a reading,
        before anything that *is* measured.
        """
        return self.configure(point, probe=True)

    def _probe_knobs(self, knobs: dict, quantity: Quantity,
                     frequency_hz: float) -> dict:
        """The knobs a probe runs with.  Default: integration capped at
        :data:`PROBE_MAX_NPLC`."""
        nplc = knobs.get("nplc")
        if nplc is not None and float(nplc) > PROBE_MAX_NPLC:
            knobs["nplc"] = PROBE_MAX_NPLC
        return knobs

    def configure(self, point, *, probe: bool = False) -> MeterConfig:
        """Select function, range and the model's knobs for *point*.

        *probe* is :meth:`configure_probe`'s fast variant."""
        # Drop the previous configuration first: a configure() that fails
        # part-way must not leave a stale range and resolution attached to the
        # readings that follow it.
        self._config = None
        self._quantity = None
        self._function = None
        self._range_spec = None
        self._func = None
        quantity = Quantity(point.quantity)
        connection = Connection(_attr(point, "connection", Connection.TWO_WIRE))
        if not self.supports(quantity, connection):
            raise AdapterError(
                f"{self._model_id} cannot measure {quantity.name} in "
                f"{connection.value}; do not plan points it cannot run")
        function = self._spec.function(quantity)
        frequency = float(_attr(point, "frequency_hz", 0.0))
        # Mirror Fluke5700ASource.validate(): the same plan typo is caught
        # meter-side, so it does not need a calibrator in the loop to surface.
        if frequency and not quantity.frequency_dependent:
            raise AdapterError(
                f"{self._model_id}: {quantity.name} is a dc quantity but the "
                f"point states {frequency:g} Hz")
        if quantity.frequency_dependent and not frequency > 0.0:
            raise AdapterError(
                f"{self._model_id}: {quantity.name} needs a frequency; the "
                f"point states {frequency:g} Hz")
        freq_key = frequency if quantity.frequency_dependent else None

        range_spec = self._select_range(function, point)
        if not range_spec.allows(connection):
            raise AdapterError(
                f"{self._model_id}: {connection.value} is not available on the "
                f"{range_spec.fs:g} {quantity.unit} range")
        mode = self._select_mode(function, range_spec, point, freq_key)
        knobs = _merged_knobs(function, range_spec, mode, point)
        if probe:
            knobs = self._probe_knobs(dict(knobs), quantity, frequency)

        applied = dict(self._pre_function(knobs, point, frequency))
        func = resolve_func(self._spec, quantity, connection)
        # Never ``None``: that is the drivers' shared "autorange" spelling,
        # and a range the plan did not choose is a range the record cannot
        # describe.  A range that states no range_arg is selected by its own
        # full scale.
        range_arg = (range_spec.range_arg if range_spec.range_arg is not None
                     else range_spec.fs)
        forced = _attr(point, "range_arg")
        if forced is not None:
            range_arg = float(forced)
        self._function = function
        self._range_spec = range_spec
        self._func = func
        self._set_function(func, range_arg)
        applied["autorange"] = False
        applied["range"] = range_arg
        if probe:
            applied["probe"] = True
        applied.update(self._post_function(knobs, point, frequency,
                                           range_spec=range_spec, mode=mode,
                                           applied=applied))
        applied.update(self._apply_knobs(knobs, point, frequency))
        # Last, so that a meter which rejected any of the above says so before
        # the configuration is recorded as applied.
        self._after_configure(applied, point)

        self._quantity = quantity
        self._config = MeterConfig(
            func=_func_label(func),
            range_arg=range_arg,
            range_fs=range_spec.fs,
            nplc=applied.get("nplc"),
            mode=mode.id if mode is not None else None,
            ac_filter=applied.get("ac_filter"),
            resolution=self._reading_resolution(range_spec, mode, applied),
            settle_s=self._settle_seconds(function, range_spec, applied, point),
            applied=dict(applied),
        )
        return self._config

    def _select_range(self, function: FunctionSpec, point) -> RangeSpec:
        forced = _attr(point, "range_arg")
        value = abs(float(point.value))
        if forced is not None:
            forced = float(forced)
            for candidate in function.ranges:
                arg = candidate.range_arg if candidate.range_arg is not None \
                    else candidate.fs
                if math.isclose(arg, forced, rel_tol=1e-9, abs_tol=0.0):
                    return candidate
            raise AdapterError(
                f"{self._model_id}: no {point.quantity} range with "
                f"range_arg {forced:g}")
        # ``for_meter_config=True`` is the meter-side question: which range can
        # the instrument be *set* to and still read this value.  Meters state
        # an overrange -- "20% overrange on all ranges, except 1000 Vdc, 3 A
        # range" (34401a-user.txt:9110, PDF p.216); the 3458A's own FUNC table
        # gives the 10 V range a 12 V full scale (UG p.276) -- so the 5700A's
        # 11 V point and its +300 ppm characterized 10 kohm belong on the
        # range below, with its finer LSD, its smaller ppm-of-range term and
        # its higher input impedance.  Tolerance evaluation still selects
        # strictly: an overrange is never a wider specification.
        range_spec = select_range(function, value, for_meter_config=True)
        if range_spec is None:
            raise AdapterError(
                f"{self._model_id}: no {Quantity(point.quantity).name} range "
                f"holds {value:g} {Quantity(point.quantity).unit}")
        return range_spec

    def _select_mode(self, function: FunctionSpec, range_spec: RangeSpec,
                     point, freq_key):
        wanted = _attr(point, "mode")
        if wanted:
            mode = function.mode(wanted)
            if mode is None:
                raise AdapterError(
                    f"{self._model_id}: {Quantity(point.quantity).name} has no "
                    f"mode {wanted!r}; known: "
                    f"{', '.join(function.mode_ids) or 'none'}")
            return mode
        band = select_band(range_spec, freq_key)
        return select_mode(function, band, freq_key)

    def _settle_seconds(self, function: FunctionSpec, range_spec: RangeSpec,
                        applied: Mapping[str, Any], point) -> float:
        override = _attr(point, "settle_s")
        if override is not None:
            return float(override)
        candidates = [function.settle_s or 0.0, range_spec.settle_s or 0.0,
                      float(applied.get("delay_s") or 0.0)]
        return max(candidates)

    # -- knob application (template method) -------------------------------

    def _pre_function(self, knobs: dict, point, frequency_hz: float) -> Mapping[str, Any]:
        """Knobs that must be set *before* the function is selected.

        Pops what it applies out of *knobs* and returns it, so that the
        configuration record says what was really sent.  Default: nothing.
        """
        return {}

    def _post_function(self, knobs: dict, point, frequency_hz: float, *,
                       range_spec: RangeSpec, mode,
                       applied: Mapping[str, Any]) -> Mapping[str, Any]:
        """Settings that only take effect *after* the function is selected.

        The 3458A's ``RES`` is the case this exists for: a function command
        with its ``%_resolution`` parameter omitted applies that function's
        **default** resolution (UG p.327), so a resolution sent beforehand is
        thrown away by the very command that selects the range.
        """
        return {}

    def _after_configure(self, applied: Mapping[str, Any], point) -> None:
        """Last check before the configuration is recorded.  Default: none."""

    def _reading_resolution(self, range_spec: RangeSpec, mode,
                            applied: Mapping[str, Any]) -> float | None:
        """One LSD of the configuration just applied.

        The model file's per-range ``resolution`` by default.  A meter whose
        LSD depends on how it was configured -- not just on which range --
        overrides this; see :meth:`HP3458AMeter._reading_resolution`.
        """
        return range_spec.resolution

    def _set_function(self, func, range_arg: float | None) -> None:
        """Select the function on a **fixed** range.

        ``-1`` is the shared 'autorange' spelling across these drivers, and
        this refuses to send it: Global Constraint §9 says every point is read
        on a range the record can name.
        """
        if range_arg is None:
            raise AdapterError(
                f"{self._model_id}: refusing to select {func!r} with no "
                f"range.  Autorange would choose the range from the signal, "
                f"and the range sets the ppm-of-range term, the resolution "
                f"and the input impedance the verdict is computed from")
        self._dev.set_func(func, range_arg)

    def _apply_knobs(self, knobs: Mapping[str, Any], point,
                     frequency_hz: float) -> dict:
        applied: dict[str, Any] = {}
        for name in _knob_order(knobs):
            value = knobs[name]
            outcome = self._apply_knob(name, value, point, frequency_hz)
            if outcome is False:
                raise AdapterError(
                    f"{self._model_id}: the model file asks for {name}="
                    f"{value!r} but this adapter cannot apply it.  Silently "
                    f"skipping it would make the specification column a lie")
            if outcome is True:
                applied[name] = value
            else:
                # A knob that was applied *differently* records what really
                # happened, plus its reason -- see the 3458A's OCOMP.
                applied.update(outcome)
        return applied

    def _apply_knob(self, name: str, value, point,
                    frequency_hz: float) -> "bool | Mapping[str, Any]":
        """Apply one knob.

        ``True`` means "applied as asked" and the knob is recorded verbatim;
        ``False`` means this adapter cannot apply it at all, which is an
        error.  A knob that was applied *differently* -- because the selected
        range does not offer it -- returns the mapping to record instead, so
        that the configuration column says what was really in force.
        """
        if name == "nplc":
            self._dev.nplc = float(value)
            return True
        if name == "delay_s":
            self._dev.delay = float(value)
            return True
        return False

    # -- measurement ------------------------------------------------------

    def read(self, n: int, *, discard: int = 0) -> Reading:
        """Take *n* samples (plus *discard* thrown away first).

        Overload sentinels are excluded from the mean and counted, using the
        *model's own* sentinel: ``1e35`` is an overload on a 1281 and an
        ordinary number on a 3458A.
        """
        if n <= 0:
            raise ValueError(f"n must be >= 1, got {n}")
        if discard < 0:
            raise ValueError(f"discard must be >= 0, got {discard}")
        if self._config is None:
            # Not a convenience check.  Reading.resolution is the input to
            # uncertainty.resolution_component, so a None here would quietly
            # drop a component out of the budget -- which shrinks U, shrinks
            # the guard band and makes a PASS *more* likely.  Failing loudly is
            # the only safe direction.
            raise AdapterError(
                f"{self._model_id}: read() before configure(); the range and "
                f"its resolution are not known, and a reading without them "
                f"cannot carry its quantisation uncertainty")
        samples = list(self._capture(n + discard))
        if discard:
            samples = samples[discard:]
        unit = self._quantity.unit if self._quantity else ""
        return Reading.from_samples(samples, overload=self._spec.is_overload,
                                    discarded=discard,
                                    resolution=self._config.resolution,
                                    unit=unit)

    def _capture(self, n: int) -> Sequence[float]:
        """One batch of raw samples, sentinels and all."""
        return self._dev.read_samples(n)

    # -- range verification -----------------------------------------------

    def range_readback(self) -> float | None:
        """Full scale of the range the **instrument** says it is on.

        In the same units as :attr:`~caldmm.cal.roles.MeterConfig.range_fs`,
        so the engine can compare the two before energizing anything: a meter
        that quietly autoranged, or that never received the range command, is
        then caught with the output still in standby rather than by a row of
        readings on a range nobody chose.

        ``None`` means the instrument cannot report its range at all (the
        1281).  It never means "the read failed" -- that raises.
        """
        return None

    def _range_query(self, command: str) -> float:
        """Ask *command* and turn the answer into a range full scale."""
        if self._range_spec is None or self._function is None:
            raise AdapterError(
                f"{self._model_id}: range_readback() before configure(); "
                f"there is no configured range to read back")
        answer = self._dev.query(command)
        try:
            reported = float(str(answer).strip())
        except (TypeError, ValueError):
            raise AdapterError(
                f"{self._model_id}: {command} answered {answer!r}, which is "
                f"not a number") from None
        return self._snap_range_fs(reported, command)

    def _snap_range_fs(self, reported: float, command: str) -> float:
        """The declared range *reported* names, by its full scale.

        Instruments do not agree on which number a range *is*.  The SCPI
        meters answer the nominal (``+1.00000000E+01`` for the 10 V range).
        The 3458A's ranges are *entered* as a maximum input and its own FUNC
        table calls the 10 V range's full scale 12 V and the 1 A range's
        1.05 A (UG p.325); ``RANGE?`` is documented only as returning "the
        present measurement range" (UG p.327), without saying which of the two
        numbers that is.

        Both name the same range, so the answer is snapped onto the model
        file's own ``fs`` — the nearest by ratio.  That is unambiguous while
        ranges are a decade apart (12 is 1.2x the 10 V range and 0.12x the
        100 V one), it does not depend on the undocumented spelling, and it
        leaves a genuine mismatch — 120 answered for a configured 10 V range —
        plainly visible as 100 V.
        """
        ranges = [r for r in self._function.ranges if r.fs > 0.0]
        if not ranges or not math.isfinite(reported) or reported <= 0.0:
            raise AdapterError(
                f"{self._model_id}: {command} answered {reported!r}; no "
                f"declared range matches it")
        best = min(ranges, key=lambda r: abs(math.log(reported / r.fs)))
        return best.fs

    # -- optional capabilities --------------------------------------------

    def internal_temperature(self) -> float | None:
        """The meter's own internal temperature in °C, or ``None``.

        ``None`` means "this meter has no sensor" -- the honest default, and
        distinct from a read that failed (the engine turns a failure into
        ``None`` too, but at its own seam).  Overridden where the hardware
        offers one; see :class:`~caldmm.cal.roles.MeterRole` for why this is
        an optional capability rather than a protocol member.
        """
        return None

    # -- construction -----------------------------------------------------

    @classmethod
    def open(cls, address: str, *, name: str | None = None, **kwargs):
        raise NotImplementedError(
            f"{cls.__name__} does not know how to open its driver")


def _drain_scpi_errors(device, model_id: str) -> None:
    """Read an SCPI meter's error queue empty and raise on anything in it.

    ``applied`` exists so that the record cannot lie about the configuration,
    and it is built from what the adapter *sent*.  The instrument's error
    queue is the only place that says whether any of it was accepted: both
    meters hold twenty errors and replace the last with ``-350, "Too many
    errors"`` when they overflow (34401a-user.txt 3366-3375, PDF p.86;
    34420a-user-guide.txt 3426-3437, PDF p.112).

    ``*OPC?`` first, so the queue is read after the configuration commands
    have actually been executed rather than while they are still in flight.
    Anything found raises: a point measured against a configuration the meter
    rejected is worse than no point at all.
    """
    complete = getattr(device, "opc", None)
    if callable(complete):
        complete()
    drain = getattr(device, "error_queue", None)
    if not callable(drain):
        raise AdapterError(
            f"{model_id}: the driver offers no error_queue(); the "
            f"configuration record could not be checked against the "
            f"instrument's own error queue")
    errors = [str(entry) for entry in (drain() or ())]
    if errors:
        raise AdapterError(
            f"{model_id}: the meter rejected part of the configuration - "
            f"SYST:ERR? returned {'; '.join(errors)}.  The knobs this point "
            f"was to be measured with are not the knobs that are in force")


class HP3458AMeter(MeterAdapter):
    """HP/Agilent/Keysight 3458A.

    ``SETACV``, ``ACBAND``, ``LFILTER`` and ``SSRC`` are sent **before** the
    function is selected — the manual's order, and the driver's own
    ``set_acv_method`` docstring says so.  Setting the technique afterwards
    re-triggers configuration and the reading that comes back is not the one
    the specification column describes.  ``RES`` is the exception that goes
    *after*: the function command resets the resolution to that function's
    default (UG p.327).

    ``AZERO`` and ``OCOMP`` go through the driver's ``set_autozero`` /
    ``set_ocomp``, which keep its cached ``autozero`` / ``ocomp`` attributes
    truthful; a driver old enough to lack them still gets the raw commands.
    """

    MODEL_ID = "hp3458a"

    #: ``%_resolution`` for sampled ac in ``SETACV SYNC``.  0.001 is 7.5
    #: digits — the datasheet's own "best accuracy 0.010 %" for this technique
    #: is 0.007 + 0.002 + **0.001**, i.e. it *assumes* this setting, and the
    #: p.26 additional-error table charges "(Resolution in %) x 1" of reading
    #: for a crest factor of 1–2.  Leaving the default 0.01 % in place adds an
    #: unbudgeted 100 ppm of reading, which at 1 V / 1 kHz is larger than the
    #: whole base specification.  The price is time: datasheet p.27 allows up
    #: to 32 s per reading here (plus the ACBAND-low term), inside the
    #: driver's own 60 s read timeout.  Settable per instance for a caller who
    #: would rather have 6.5 digits and the speed.
    sync_resolution_pct: float = 0.001

    #: ``%_resolution`` -> digits for ``SETACV SYNC`` (UG RES entry, p.330:
    #: "0.001 = 7.5 digits; 0.01 = 6.5 digits; 0.1 = 5.5 digits; and 1 = 4.5").
    _SYNC_DIGITS = {0.001: 7.5, 0.01: 6.5, 0.1: 5.5, 1.0: 4.5}
    #: RNDM "has a fixed resolution of 4.5 digits that cannot be changed"
    #: (ibid.), which is exactly the datasheet's p.30 resolution column.
    _RNDM_DIGITS = 4.5
    #: The analog technique's own maximum-resolution column (datasheet p.28)
    #: is one decade coarser than the SYNC one on every range but 10 mV —
    #: 6.5 digits of the range.
    _ANA_DIGITS = 6.5

    #: The 3458A "can only perform offset compensation on the 10 Ohm through
    #: 100 kOhm ranges; offset compensation does not function on the other
    #: ranges" (UG p.96, 3458a-user-guide.txt 3345-3347).  The model file asks
    #: for OCOMP across the whole resistance function.
    OCOMP_RANGE_OHMS = (10.0, 100.0e3)

    #: Above this tone the level filter comes off: the datasheet's "LFILTER ON
    #: recommended" note 3 covers the SYNC columns to 50 kHz (p.25) and the
    #: filter's own 3 dB point is 75 kHz (UG p.283).
    LFILTER_MAX_HZ = 50.0e3

    def __init__(self, device, **kwargs):
        super().__init__(device, **kwargs)
        # (function, range) the last configure() left selected, and when it
        # was selected -- the datasheet's dc figures are written for a range
        # held longer than four minutes.  See _post_function.
        self._range_key: tuple | None = None
        self._range_since: float | None = None

    def _pre_function(self, knobs: dict, point, frequency_hz: float) -> Mapping[str, Any]:
        if not Quantity(point.quantity).frequency_dependent:
            # Leave any ac knob in place rather than dropping it silently: on a
            # dc point it is a model-file error and _apply_knobs will say so.
            return {}
        applied: dict[str, Any] = {}
        method = knobs.pop("ac_method", None)
        if method is not None:
            method = str(method).upper()
            self._dev.set_acv_method(method)
            applied["ac_method"] = method
        # ACBAND is not optional on an ac point: it governs the settling and
        # sampling the meter chooses, and above 2 MHz it selects a different
        # accuracy table entirely.  Absent an explicit band, declare the tone.
        low, high = _band_edges(knobs.pop("ac_band", None), frequency_hz)
        self._dev.set_acband(low, high)
        applied["ac_band"] = (low, high)
        applied.update(self._sync_controls(method, frequency_hz))
        return applied

    def _sync_controls(self, method: str | None,
                       frequency_hz: float) -> Mapping[str, Any]:
        """``SSRC LEVEL,HOLD`` and the level filter, for a SYNC point.

        Both are power-on defaults the ``PRESET NORM`` table never touches
        (UG p.319), and both matter only to the synchronous technique:

        * ``SSRC``'s mode parameter "applies only to synchronous ACV or
          ACDCV" (UG p.353), and its default ``AUTO`` means that "if the input
          signal is removed during a reading and does not return within a
          certain amount of time, the measurement method changes to random so
          that the reading can be completed".  A reading finished in random
          sampling is up to fifteen times looser than the SYNC row it would be
          recorded and judged against, and nothing in the answer says so.
          ``HOLD`` turns that silent degradation into an error.
        * ``LFILTER`` puts a 75 kHz low-pass on the *level-detection* path
          (UG p.283), which is what the sync trigger looks at; the datasheet
          marks every SYNC column from 1 Hz to 50 kHz "LFILTER ON
          recommended" (p.25 note 3).

        Neither is touched for ANA or RNDM, where neither does anything: a
        knob that had no effect must not appear in the record as though it
        had.
        """
        if method != "SYNC":
            return {}
        wanted = frequency_hz <= self.LFILTER_MAX_HZ
        for name in ("set_sync_source", "set_lfilter"):
            if not callable(getattr(self._dev, name, None)):
                raise AdapterError(
                    f"{self._model_id}: this driver has no {name}(), so a "
                    f"SYNC point cannot be given SSRC LEVEL,HOLD and its "
                    f"LFILTER; the row would claim the SYNC accuracy table "
                    f"while a lost sync silently completed in random sampling")
        self._dev.set_sync_source("LEVEL", "HOLD")
        self._dev.set_lfilter(wanted)
        return {"sync_source": "LEVEL,HOLD", "lfilter": wanted}

    def _set_function(self, func, range_arg: float | None) -> None:
        # ARANGE's power-on control is ON (UG p.240), and it is a single
        # global setting rather than a per-function one, so it is asserted on
        # every point rather than assumed from the last one.
        self._dev.cmd("ARANGE OFF")
        # And so is MATH.  ``_range_hold`` records ``math_null: False`` on
        # every dc row -- the datasheet's p.12 footnote 5 regime, worth
        # 0.15-0.7 ppm of range -- and until this was sent that record was an
        # assumption about a state nobody had asserted: ``PRESET NORM`` leaves
        # MATH OFF, but a front panel or an earlier session can leave a NULL
        # registered, and the row would then claim a regime it was not in.
        self._dev.cmd("MATH OFF")
        super()._set_function(func, range_arg)

    def _post_function(self, knobs: dict, point, frequency_hz: float, *,
                       range_spec: RangeSpec, mode,
                       applied: Mapping[str, Any]) -> Mapping[str, Any]:
        method = applied.get("ac_method")
        recorded: dict[str, Any] = {}
        if method == "SYNC":
            digits = self._SYNC_DIGITS.get(float(self.sync_resolution_pct))
            if digits is None:
                raise AdapterError(
                    f"{self._model_id}: sync_resolution_pct="
                    f"{self.sync_resolution_pct!r} is not one of the "
                    f"%_resolution values SETACV SYNC accepts "
                    f"({', '.join(f'{v:g}' for v in self._SYNC_DIGITS)}; "
                    f"UG p.330)")
            # After the function command, which would otherwise reset the
            # resolution to the SYNC default of 0.01 % (UG p.327).
            self._dev.cmd(f"RES {self.sync_resolution_pct:g}")
            recorded["resolution_pct"] = float(self.sync_resolution_pct)
            recorded["resolution_digits"] = digits
        elif method == "RNDM":
            recorded["resolution_digits"] = self._RNDM_DIGITS
        if Quantity(point.quantity) is not Quantity.RES:
            # The driver connects with OCOMP ON, and only a resistance point
            # sets it; left on, a DCI burst never reaches MEM ("MCOUNT? kept
            # returning sample readings"). Offset compensation is a
            # resistance technique (UG p.96), so it goes off everywhere else.
            setter = getattr(self._dev, "set_ocomp", None)
            if callable(setter):
                setter(False)
            else:
                self._dev.cmd("OCOMP OFF")
        recorded.update(self._range_hold(range_spec))
        return recorded

    def _range_hold(self, range_spec: RangeSpec) -> Mapping[str, Any]:
        """How long this range has been selected, and that NULL was not used.

        Datasheet p.12 footnote 5: the dc figures assume "MATH NULL and fixed
        range"; without MATH NULL add 0.15 ppm of range on 10 V and 0.7 ppm on
        0.1 V, and on a fixed range held **less than four minutes** add 0.25 /
        1.7 / 17 ppm of range on 10 V / 1 V / 0.1 V.  ``PRESET NORM`` leaves
        MATH OFF and nothing here sends ``MATH NULL``, and a verification
        walks its ranges far faster than four minutes apiece — so rather than
        let every dc row sit silently in the worst regime, the row records
        which regime it was: ``math_null: False`` always, and the seconds this
        exact (function, range) has been continuously selected.

        The hold clock is reset when the *selected range changes*.  Whether
        re-issuing an unchanged ``DCV 1.00E+01`` restarts the instrument's own
        four-minute clock is not documented and cannot be read back, so this
        is the honest lower bound on what the adapter knows.
        """
        key = (_func_label(self._func), range_spec.fs)
        now = _now()
        if key != self._range_key:
            self._range_key = key
            self._range_since = now
        held = 0.0 if self._range_since is None else now - self._range_since
        return {"math_null": False, "range_held_s": held}

    def range_readback(self) -> float | None:
        """``RANGE?`` — "returns the present measurement range" (UG p.327).

        Not ``ARANGE?``: that reports the autorange *mode*, which the same
        page is careful to say ``RANGE?`` does not.  The answer is a maximum
        input (12 on the 10 V range), which :meth:`_snap_range_fs` maps back
        onto the model file's nominal full scale.
        """
        return self._range_query("RANGE?")

    def _reading_resolution(self, range_spec: RangeSpec, mode,
                            applied: Mapping[str, Any]) -> float | None:
        """The LSD this configuration really gives, not the range's best one.

        The model file's per-range ``resolution`` is the **SYNC maximum** (the
        datasheet's p.25 column).  Read the same range in ANA and the LSD is a
        decade coarser (p.28); in RNDM, three decades (p.30); and in SYNC at
        the default 0.01 % it is a decade coarser than the file says.  Since
        ``Reading.resolution`` is what the budget's quantisation component is
        built from, recording the range's best figure for a reading taken
        another way understates it.

        Digits are converted the way the datasheet's own tables read: an
        n-digit reading of a range resolves it to ``fs * 10 ** (0.5 - n)``,
        floored at the range's stated maximum resolution (the 10 mV ac range
        is capped at 10 nV rather than the 1 nV the arithmetic would give).
        Every cell of the three tables comes out right; a test pins them.
        """
        digits = applied.get("resolution_digits")
        if digits is None and applied.get("ac_method") == "ANA":
            digits = self._ANA_DIGITS
        if digits is None:
            return range_spec.resolution
        floor = range_spec.resolution or 0.0
        return max(floor, range_spec.fs * 10.0 ** (0.5 - float(digits)))

    def _apply_knob(self, name: str, value, point, frequency_hz: float):
        if name == "autozero":
            setter = getattr(self._dev, "set_autozero", None)
            if callable(setter):
                setter(_as_bool(value))
            else:
                self._dev.cmd(f"AZERO {'ON' if _as_bool(value) else 'OFF'}")
            return True
        if name == "ocomp":
            return self._apply_ocomp(_as_bool(value))
        return super()._apply_knob(name, value, point, frequency_hz)

    def _apply_ocomp(self, wanted: bool):
        """OCOMP, but only where the instrument implements it.

        The model file asks for offset compensation across the whole
        resistance function; the meter performs it on 10 Ω to 100 kΩ only
        (UG p.96).  Sending ``OCOMP ON`` on the 1 MΩ range changes nothing
        and recording it as applied claims a measurement condition that was
        not in force — the exact misrepresentation ``_apply_knobs`` exists to
        prevent.  The high ranges' specifications do not depend on it, so this
        is a record fix rather than a measurement one.
        """
        low, high = self.OCOMP_RANGE_OHMS
        fs = self._range_spec.fs if self._range_spec is not None else 0.0
        available = wanted and low <= fs <= high
        setter = getattr(self._dev, "set_ocomp", None)
        if callable(setter):
            setter(available)
        else:
            self._dev.cmd(f"OCOMP {'ON' if available else 'OFF'}")
        if available == wanted:
            return {"ocomp": wanted}
        return {
            "ocomp": False,
            "ocomp_note": (
                f"the 3458A performs offset compensation on 10 Ohm to 100 k"
                f"Ohm only (UG p.96); the {fs:g} Ohm range does not"),
        }

    def internal_temperature(self) -> float | None:
        """``TEMP?`` -- the 3458A's internal temperature, in °C.

        Gated on the driver's own ``has_internal_temp`` capability hook rather
        than assumed, so a driver handed in without the capability answers
        ``None`` instead of sending a query the instrument would beep at.
        A failed read raises; the engine's seam turns that into ``None``.
        """
        if not getattr(self._dev, "has_internal_temp", lambda: False)():
            return None
        return float(self._dev.get_temp())

    #: ``CAL?`` constants holding the internal temperature at the last
    #: external adjustment: 58 ``CAL 0``, 59 ``CAL 10`` (dc volts), 60
    #: ``CAL 10K`` (ohms, and therefore dc and ac current) -- Cal Manual
    #: p.57 step 3 and the ``CAL?`` reference.
    CAL_TEMPERATURE_CONSTANTS = {"cal0": 58, "dcv": 59, "ohms": 60}

    def cal_temperatures(self) -> dict[str, float]:
        """The adjustment temperatures, from ``CAL? 58``/``59``/``60``, in °C.

        The Cal Manual makes them a precondition of every performance test:
        "The internal temperature of the 3458A under test must be within 5
        degrees C of its temperature when last adjusted (CAL 0, CAL 10, and
        CAL 10K).  These temperatures can be determined by executing the
        commands CAL? 58, CAL? 59, CAL? 60" (p.57).  A constant that cannot
        be read is left out rather than guessed.
        """
        getter = getattr(self._dev, "get_cal_const", None)
        if not callable(getter):
            return {}
        out: dict[str, float] = {}
        for name, number in self.CAL_TEMPERATURE_CONSTANTS.items():
            try:
                out[name] = float(getter(number))
            except Exception:                          # noqa: BLE001 - optional
                continue
        return out

    def autocal(self, kind: str = "ALL") -> None:
        """``ACAL <kind>`` and wait for it to finish (ALL takes ~16 min).

        Only ever called with the calibrator in standby: "Always disconnect
        any AC input signals before you perform an autocal" (Cal Manual,
        ``ACAL`` reference).  The driver sleeps the manual's stated duration;
        the post-ACAL settling time is the caller's.
        """
        from caldmm.drivers.hp3458a import ACAL
        self._dev.acal(ACAL[str(kind).strip().upper()])

    @classmethod
    def open(cls, address: str, *, name: str | None = None, **kwargs):
        from caldmm.drivers.hp3458a import HP3458A
        bus, pad = parse_gpib_address(address)
        return cls(HP3458A(gpib_address=pad, gpib_bus=bus, name=name,
                           **kwargs))


# --------------------------------------------------------------------------
# Sources
# --------------------------------------------------------------------------

class Fluke5700ASource:
    """Fluke 5700A/5720A Series II as a :class:`~caldmm.cal.roles.SourceRole`.

    :meth:`apply` is ``set_output`` → ``operate`` → settle → read back, and the
    read-back is the point of the whole class.  ``OUT?`` in the resistance
    function returns the **characterized** value the calibrator is really
    sourcing — up to 500 ppm from the nominal by design — and that value, not
    the request, is the reference for the error.  The nominal is what the plan
    asked for; it is not what came out of the terminals.

    ``UNCERT?`` is the calibrator's own uncertainty for the output presently
    programmed, at the confidence ``CAL_CONF`` selects and over the interval
    ``CAL_INTV`` selects.  It is carried on the returned state so the engine can
    build the reference component of the budget without a second lookup — and
    its ``-1.0`` "no specification here" sentinel is already ``None`` by the
    time it reaches us.
    """

    MODEL_ID = "fluke5700a"

    def __init__(self, device, *, spec: ModelSpec | None = None,
                 model_id: str | None = None):
        self._dev = device
        self._model_id = model_id or self.MODEL_ID
        self._spec = spec if spec is not None else load_model(self._model_id)
        self._confidence: float | None = None
        #: True while **this adapter** holds the range locked, so the release
        #: is driven from what it did rather than from an ``ISR?`` a front
        #: panel could have changed underneath it.
        self._range_locked = False

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def spec(self) -> ModelSpec:
        return self._spec

    @property
    def device(self):
        return self._dev

    def __repr__(self) -> str:                 # pragma: no cover - debugging aid
        name = getattr(self._dev, "name", None)
        return (f"<{type(self).__name__} {self._model_id}"
                + (f" {name!r}" if name else "") + ">")

    # -- offline checks ---------------------------------------------------

    def validate(self, point) -> list[str]:
        """Every reason this point cannot be sourced.  **No I/O.**

        Delegates the envelope to the driver's own ``validate_output``, which
        encodes §11.1–§11.3 and returns *all* violations rather than the first.
        ``options`` are installed-capability tokens (``*OPT?``) only, and
        ``range_id`` is keyword-only there because a range identifier and a
        capability string are separate namespaces: infer one from the other and
        a unit that merely *has* an amplifier starts being validated as though
        it were using it.
        """
        from caldmm.drivers.fluke5700a import validate_output
        quantity = Quantity(point.quantity)
        connection = Connection(_attr(point, "connection", Connection.TWO_WIRE))
        problems: list[str] = []
        function = self._spec.function(quantity)
        if function is None:
            problems.append(
                f"{self._model_id} does not source {quantity.name}")
        else:
            problems.extend(self._wiring_problems(function, quantity,
                                                  connection, point))
        stated = float(_attr(point, "frequency_hz", 0.0))
        if stated and not quantity.frequency_dependent:
            # Reported rather than quietly zeroed: "19 kOhm at 1 kHz" is a plan
            # that meant something this calibrator cannot deliver, and sourcing
            # dc instead would answer a question nobody asked.
            problems.append(
                f"{quantity.name} is a dc quantity but the point states "
                f"{stated:g} Hz")
        problems.extend(validate_output(
            float(point.value), quantity.unit, self._frequency(point),
            options=list(getattr(self._dev, "options", []) or []),
            range_id=_attr(point, "range_id"),
            wideband=quantity is Quantity.WBV))
        return problems

    def _wiring_problems(self, function, quantity: Quantity,
                         connection: Connection, point) -> list[str]:
        """Wiring the calibrator cannot offer here, from the model file.

        Offline, so a plan that asks for four-wire sensing on the 100 MΩ
        resistance — or for two-wire compensation above 19 kΩ — is rejected
        before anything is energized, rather than at the ``RCOMP`` that faults.
        """
        problems: list[str] = []
        if function.connections and connection not in function.connections:
            problems.append(
                f"{self._model_id} does not source {quantity.name} in "
                f"{connection.value}")
            return problems
        range_spec = select_range(function, abs(float(point.value)))
        if range_spec is not None and not range_spec.allows(connection):
            problems.append(
                f"{connection.value} is not available on the "
                f"{range_spec.range_id or f'{range_spec.fs:g}'} range")
        return problems

    @staticmethod
    def _frequency(point) -> float:
        """0 Hz for every dc and resistance point, whatever the point says.

        Amplitude and frequency travel together in one ``OUT``; a residual
        frequency from the previous setpoint is what turns ``OUT 100V`` into
        100 V at 1 MHz, and any frequency at all in the ohms function is
        fault 812.
        """
        quantity = Quantity(point.quantity)
        if not quantity.frequency_dependent:
            return 0.0
        return float(_attr(point, "frequency_hz", 0.0))

    # -- lifecycle --------------------------------------------------------

    def apply(self, point) -> SourceState:
        """Source the point and report what is actually on the terminals."""
        from caldmm.drivers.fluke5700a import ISR_OPER, ISR_SETTLED, decode_isr

        problems = self.validate(point)
        if problems:
            raise AdapterError(
                f"refusing to source {point.value!r} "
                f"{Quantity(point.quantity).unit}: " + "; ".join(problems))

        quantity = Quantity(point.quantity)
        frequency = self._frequency(point)
        wideband = quantity is Quantity.WBV
        if self._dev.wideband != wideband:
            # Never switch output functions with the output live; WBAND OFF
            # can re-select a standard-function value on the terminals
            # ([OM §4-30]).  set_output sends its own STBY again, cheaply.
            self._dev.standby()
            self._dev.wband(wideband)
        locked = self._range_to_lock(point, quantity, frequency)
        if locked is None:
            self._dev.set_output(float(point.value), quantity.unit, frequency)
        else:
            self._source_on_locked_range(point, quantity, frequency, *locked)
        self._set_modifiers(quantity, point)
        self._dev.operate(settle=True)

        # Read the status back rather than inferring it from "operate() did
        # not raise": OPER is silently inhibited above 22 V while a fault is
        # queued, and several conditions trip the output back to standby.
        isr = self._dev.query_int("ISR?")
        actual, unit, out_frequency = self._dev.out_actual()
        uncertainty = self._uncertainty()
        return SourceState(
            actual_value=actual,
            unit=unit,
            frequency_hz=out_frequency,
            range_id=self._dev.range_id(),
            uncertainty=uncertainty,
            settled=bool(isr & ISR_SETTLED),
            operate=bool(isr & ISR_OPER),
            raw=decode_isr(isr),
        )

    # -- the range lock (H05) ---------------------------------------------

    def _range_to_lock(self, point, quantity: Quantity,
                       frequency_hz: float) -> "tuple[str, float] | None":
        """``(range_id, anchor)`` when this point needs ``RANGELCK``.  No I/O.

        ``None`` means the calibrator's own autoranging already lands on the
        range the plan named, which is the common case — 100 % points, and
        every resistance, since each catalogue value *is* a range.

        Raises :class:`AdapterError` when the plan pins a range no output can
        select at this frequency: that point cannot be realised as written, and
        the alternative is sourcing it on the range below and reporting it
        against this one.  Raised **before** any I/O, so nothing is programmed.
        """
        range_id = _attr(point, "range_id")
        if not range_id:
            return None
        function = self._spec.function(quantity)
        if function is None:
            return None
        range_spec = next((r for r in function.ranges
                           if r.range_id == range_id), None)
        if range_spec is None:
            return None                   # already reported by validate()
        if not needs_range_lock(function, range_spec, float(point.value)):
            return None
        anchor = range_lock_anchor(function, range_spec,
                                   frequency_hz=frequency_hz)
        if anchor is None:
            raise AdapterError(
                f"{self._model_id}: {point.value!r} {quantity.unit} is pinned "
                f"to the {range_id} range, which needs a range lock, and no "
                f"output selects that range"
                + (f" at {frequency_hz:g} Hz" if frequency_hz else "")
                + ".  Refusing to source it on the range below and report it "
                  "against this one")
        return (range_id, anchor)

    def _source_on_locked_range(self, point, quantity: Quantity,
                                frequency_hz: float, range_id: str,
                                anchor: float) -> None:
        """Anchor the range, lock it, then send the setpoint.  §3-16's order.

        Every step happens in **standby** — ``set_output`` opens with ``STBY``
        and ``OPER`` is the caller's next move — so an anchor of 1100 V is
        never on the terminals.  Both ``RANGE?`` reads are verifications, not
        bookkeeping: if either disagrees the point is abandoned before the
        output is energized, because a lock that did not take means the
        measurement would be made on one range and credited to another.
        """
        unit = quantity.unit
        self._dev.set_output(anchor, unit, frequency_hz)
        selected = self._dev.range_id()
        if selected != range_id:
            raise AdapterError(
                f"{self._model_id}: {anchor:g} {unit} was meant to select the "
                f"{range_id} range for a range lock, but RANGE? answers "
                f"{selected!r}.  The output is in standby and the point was "
                f"not sourced")
        self._dev.range_lock(True)
        self._range_locked = True
        self._dev.set_output(float(point.value), unit, frequency_hz,
                             keep_range_lock=True)
        held = self._dev.range_id()
        if held != range_id:
            self._release_range_lock()
            raise AdapterError(
                f"{self._model_id}: the range lock on {range_id} did not hold "
                f"— RANGE? answers {held!r} after sourcing {point.value!r} "
                f"{unit}.  The point would have been measured on {held} and "
                f"credited to {range_id}")

    def _release_range_lock(self) -> None:
        """``RANGELCK OFF``, but only if this adapter turned it on.

        "Deactivate Range Lock before setting the next voltage output"
        ([SM 2007] Table 3-19 note 3): a lock left on turns the next larger
        setpoint into a fault instead of a range change.
        """
        if not self._range_locked:
            return
        self._range_locked = False
        self._dev.range_lock(False)

    def _set_modifiers(self, quantity: Quantity, point) -> None:
        """Assert the sensing modifiers this point requires — always, both ways.

        Four-wire on the calibrator is ``EXTSENSE ON`` and two-wire
        compensation is ``RCOMP ON``; sourcing 4-wire ohms without external
        sense silently delivers the 2-wire value, which is a wrong reference
        rather than a failed run.

        Every other function is ``2w`` in the model file, so both modifiers are
        driven **off** there rather than left alone.  Leaving ``EXTSENSE`` on
        after an ohms point is not a cosmetic leak: external sense with no
        signal at the SENSE terminals trips the output to standby (§11.7), so
        the next volts point fails inside ``operate()`` with no hint that the
        adapter left a modifier set.  Asserting both also means a session that
        inherits a front-panel ``EXTSENSE ON`` is corrected rather than
        obeyed — the adapter states the configuration it requires instead of
        assuming it.

        Called after ``OUT`` because ``RCOMP`` is only legal once a resistance
        output of 19 kΩ or less is selected; switching it off is always allowed.
        """
        if quantity is not Quantity.RES:
            self._dev.ext_sense(False)
            self._dev.rcomp(False)
            return
        connection = Connection(_attr(point, "connection", Connection.TWO_WIRE))
        self._dev.ext_sense(connection is Connection.FOUR_WIRE)
        self._dev.rcomp(connection is Connection.TWO_WIRE_RCOMP)

    def _uncertainty(self) -> Uncertainty | None:
        value, unit, days = self._dev.uncert()
        if value is None:
            return None                      # the -1.0 "no specification" case
        return Uncertainty(value=value, unit=unit,
                           confidence=self._cal_confidence(),
                           interval_days=days)

    def _cal_confidence(self) -> float | None:
        """``CAL_CONF?`` as a fraction, cached for the session.

        Read from the instrument, never assumed: it is a stored setting that
        changes what ``UNCERT?`` answers for the very same output, and hard
        coding 95 % where the unit is set to 99 % understates the reference
        contribution by a third.  An answer that is neither is an error rather
        than a default, for exactly that reason — a fallback to 95 % would
        reintroduce the failure this read exists to prevent.
        """
        if self._confidence is None:
            level = str(self._dev.cal_conf()).strip().upper()
            if level.endswith("99"):
                self._confidence = 0.99
            elif level.endswith("95"):
                self._confidence = 0.95
            else:
                raise AdapterError(
                    f"{self._model_id}: CAL_CONF? answered {level!r}; expected "
                    f"CONF95 or CONF99.  The confidence UNCERT? was computed "
                    f"at cannot be guessed")
        return self._confidence

    def cal_temperature(self) -> float | None:
        """``CAL_TEMP? CAL`` -- the ambient the stored constants assume, in °C.

        The 5700A has no live internal sensor; what it records is the
        temperature entered (or defaulted) at its last full calibration, and
        the bench-ambient-minus-``CAL_TEMP`` delta is the quantity its tempco
        specification is written against.  Recorded in the run header as
        ``source_cal_temp_c``.  A failed query raises; the runner reads this
        through a guard that turns it into "not recorded".
        """
        value = self._dev.cal_temp_query("CAL")
        return None if value is None else float(value)

    def standby(self) -> None:
        """De-energize, and verify the output really came off.

        The driver's ``standby()`` reads the OPER bit back: ``STBY`` is
        "ignored if not in remote" ([OM Table 5-2, p.190]) and logs fault
        2213, so an operator's "Go To Local Control" mid-run would otherwise
        turn every de-energize in the run into a silent no-op.  It still does
        not drain the fault queue, so it stays safe to call redundantly, but
        it **can** raise now: a caller in a ``finally`` that loses its
        original exception to "the output may still be live" has been told the
        more important of the two.

        The range lock is released **after** the de-energize, and only if this
        adapter took it: safety first, tidiness second, and a ``RANGELCK OFF``
        that raised would otherwise hide a standby that did not.
        """
        self._dev.standby()
        self._release_range_lock()

    def zero_output(self) -> None:
        """Program a zero output — the manual's own pre-rewiring step (M52).

        "Press RESET, or 0 V ENTER.  Either action sets the calibrator to 0 mV
        in standby" ([OM 1996] lines 4311-4313, PDF p.102).  The engine calls
        this immediately before every :meth:`standby`, so a group boundary
        hands the operator an output that is both off *and* zero rather than
        1100 V still selected, one ``OPR`` press away.

        The range lock goes first: 0 V is below the bottom of most locked
        ranges, and a setpoint outside the locked range is a coupled-command
        fault ([OM §5-17]) — the very thing the lock is supposed to prevent.
        The wideband output goes with it, because 0 V is not a legal wideband
        output at all (its minimum is 300 µV and it needs a frequency), so a
        zero after a wideband group would refuse rather than de-energize.
        The output is left **staged**, not de-energized; ``standby()`` is what
        de-energizes, and it is called right after.
        """
        self._release_range_lock()
        if self._dev.wideband:
            # Never switch output functions with the output live ([OM §4-30]).
            self._dev.standby()
            self._dev.wband(False)
        self._dev.set_output(0.0, Quantity.DCV.unit, 0.0)

    # -- current output post (H02 / H04) ----------------------------------

    def current_post(self) -> str:
        """``CUR_POST?`` — which binding posts the current output leaves by.

        One of :data:`CURRENT_POSTS`.  Worth asking before every dc/ac current
        group: the setting is nonvolatile and ``*RST`` does not touch it
        ([OM Table 5-2, p.176]), the factory default is ``NORMAL`` — the
        **OUTPUT HI** post, not the AUX CURRENT OUTPUT post — and no meter can
        tell the difference between a current sourced from the other post and
        a broken lead.  What it reports is what the operator prompt must name.
        """
        return str(self._dev.cur_post()).strip().upper()

    def set_current_post(self, post: str) -> str:
        """Select the current output posts and confirm the calibrator took it.

        Returns the post the instrument reports afterwards.  The read-back is
        the point: ``CUR_POST`` is "ignored if not in remote", and a
        rear-output calibrator cannot use ``AUX`` at all ([OM §4-27, p.114]:
        "If AUX is selected for a calibrator configured for rear output, error
        message: 'Compliance voltage exceeded' will be displayed"), which
        shows up when current is sourced rather than when the post is
        selected.  Raises :class:`AdapterError` if the two disagree.
        """
        wanted = str(post).strip().upper()
        if wanted not in CURRENT_POSTS:
            raise AdapterError(
                f"{self._model_id}: {post!r} is not a current output post; "
                f"the calibrator has {', '.join(CURRENT_POSTS)}")
        self._dev.set_cur_post(wanted)
        actual = self.current_post()
        if actual != wanted:
            raise AdapterError(
                f"{self._model_id}: CUR_POST {wanted} did not take effect - "
                f"CUR_POST? answers {actual!r}.  Current would leave by the "
                f"{actual} binding post while the wiring instruction names "
                f"{wanted}")
        return actual

    def health(self) -> SourceHealth:
        """Fault queue, output state, warm-up and calibration ages.

        Draining the fault queue is deliberate, not a side effect: a non-empty
        queue *silently inhibits* ``OPER`` for outputs of 22 V and over, and
        draining is the recommended way to clear that inhibit precisely because
        it preserves the fault information — which is returned here.
        """
        from caldmm.drivers.fluke5700a import (
            ISR_OPER, STATE_TRIPPED, decode_isr)

        faults = tuple((int(code), str(text))
                       for code, text in self._dev.drain_faults())
        isr = self._dev.query_int("ISR?")
        code, substate = self._dev.state()
        return SourceHealth(
            operate=bool(isr & ISR_OPER),
            tripped=int(code) == STATE_TRIPPED,
            faults=faults,
            warm_minutes=self._dev.ontime(),
            days_since_cal=self._dev.cal_days("CAL"),
            days_since_zero=self._dev.cal_days("ZERO"),
            state=(int(code), substate),
            raw=decode_isr(isr),
        )

    @classmethod
    def open(cls, address: str, *, name: str | None = None, **kwargs):
        """Open by GPIB address or serial port.

        ``4`` / ``gpib1,4`` is GPIB; ``/dev/ttyUSB0`` or ``/dev/ttyUSB0@19200``
        is the rear-panel RS-232C port.  The slash decides
        (:func:`is_serial_address`).
        """
        from caldmm.drivers.fluke5700a import Fluke5700A
        if is_serial_address(address):
            port, baud = parse_serial_address(address)
            return cls(Fluke5700A(serial_port=port, serial_baud=baud,
                                  name=name, **kwargs))
        bus, pad = parse_gpib_address(address)
        return cls(Fluke5700A(gpib_address=pad, gpib_bus=bus, name=name,
                              **kwargs))


# --------------------------------------------------------------------------
# Ambient temperature
# --------------------------------------------------------------------------

class StaticEnvironment:
    """A fixed ambient — an operator-entered number, or a fixture's.

    The honest default is :class:`~caldmm.cal.roles.Environment` with
    ``ambient_c=None``, which the verdict layer turns into ``AMBIENT_UNKNOWN``
    and refuses to call a PASS.  Supplying a number here is a *claim*, and it
    is recorded with its source so a report can say where it came from.
    """

    def __init__(self, ambient_c: float | None = None, *,
                 humidity_pct: float | None = None, source: str = "operator"):
        self._environment = Environment(ambient_c=ambient_c,
                                        humidity_pct=humidity_pct,
                                        timestamp=datetime.now().astimezone(),
                                        source=source)

    def read(self) -> Environment:
        return self._environment


# --------------------------------------------------------------------------
# Factory
# --------------------------------------------------------------------------

#: Model id -> adapter class.  Cross-checked against each model file's
#: ``adapter:`` field by :func:`adapter_class`, so the YAML stays authoritative
#: without this module having to import itself by name.
ADAPTERS: Mapping[str, type] = {
    "hp3458a": HP3458AMeter,
    "fluke5700a": Fluke5700ASource,
}


def adapter_class(model_id: str) -> type:
    """The adapter class for *model_id*, checked against its model file."""
    spec = load_model(model_id)
    cls = ADAPTERS.get(spec.model_id)
    if cls is None:
        raise AdapterError(
            f"no adapter for {spec.model_id!r}; known: "
            f"{', '.join(sorted(ADAPTERS))}")
    declared = (spec.adapter or "").rsplit(":", 1)[-1]
    if declared and declared != cls.__name__:
        raise AdapterError(
            f"{spec.model_id}: the model file declares adapter "
            f"{spec.adapter!r} but the registry has {cls.__name__}")
    return cls


def _open(model_id: str, instrument: str, role: Role, **kwargs):
    spec = load_model(model_id)
    if spec.role is not Role.BOTH and spec.role is not role:
        raise AdapterError(
            f"{spec.model_id} is a {spec.role.value}, not a {role.value}")
    cls = adapter_class(model_id)
    name, address = parse_instrument_spec(instrument)
    return cls.open(address, name=name, **kwargs)


def open_meter(model_id: str, instrument: str, **kwargs):
    """Open a meter: ``open_meter("hp3458a", "dmm1:22")``.

    *instrument* is the project's ``name:address`` form; *model_id* is separate
    and mandatory because an instrument name is an operator-chosen designator.
    ``dmm1`` and ``dmm2`` are unit designators distinguishing two of the
    same model — inferring a model, a channel or a role from one is exactly the
    mistake this signature makes impossible.

    Driver imports happen inside the adapter's ``open``, so this module stays
    importable with no interface libraries present.
    """
    return _open(model_id, instrument, Role.METER, **kwargs)


def open_source(model_id: str, instrument: str, **kwargs):
    """Open a source: ``open_source("fluke5700a", "5700a:4")``."""
    return _open(model_id, instrument, Role.SOURCE, **kwargs)

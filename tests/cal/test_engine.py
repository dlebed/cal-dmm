"""Tests for caldmm.cal.engine — the execution engine.

**Fake roles only.**  No driver is imported, no ``MagicMock`` is used and
nothing here touches hardware: the engine's contract is a *sequence of calls
with real values* (apply, then configure with the readback, then settle, then
read), and a mock that answers everything would assert none of it.  The fakes
below record an ordered log for exactly that reason.

The most important test in the file is the last one: ``cal.engine`` must import
with ``Gpib``, ``serial``, ``pyvisa`` and ``smbus`` blocked from import, in a
subprocess where the root conftest's stubs cannot help it.
"""

import math
import os
import statistics
import subprocess
import sys
import textwrap
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

import caldmm
from caldmm.cal.engine import (
    DEFAULT_SETTLE_MARGIN_S,
    FLAG_AMBIENT_READ_FAILED,
    FLAG_GROSS_CHECK_NOT_APPLICABLE,
    FLAG_GROSS_ERROR_ABORT,
    FLAG_LIMIT_FROM_SPEC_TABLE,
    FLAG_LOAD_DERATED,
    FLAG_LOADING_CORRECTED,
    FLAG_LOADING_UNCORRECTED_HF,
    FLAG_METER_RANGE_MISMATCH,
    FLAG_NO_REFERENCE_UNCERTAINTY,
    FLAG_NO_TYPE_A,
    FLAG_NO_VALID_SAMPLES,
    FLAG_OVERLOAD_SAMPLES,
    FLAG_POINT_FAILED,
    FLAG_PRESTEP_FAILED,
    FLAG_RANGE_MISMATCH,
    FLAG_REFERENCE_CONFIDENCE_UNKNOWN,
    FLAG_REFERENCE_FROM_SPEC_TABLE,
    FLAG_REFERENCE_OUT_OF_WINDOW,
    FLAG_SOURCE_NOT_OPERATE,
    FLAG_SOURCE_NOT_WARM,
    FLAG_SOURCE_TRIPPED_MID_POINT,
    FLAG_SOURCE_ZEROS_DUE,
    FLAG_STANDBY_NOT_CONFIRMED,
    FLAG_TEMPCO_NOT_EVALUATED,
    FLAG_THERMAL_EMF_DEFAULT,
    FLAG_THERMAL_EMF_NOT_CONVERTED,
    FLAG_WIRING_PROOF_FAILED,
    FLAG_WIRING_PROOF_NOT_RESOLVABLE,
    FLAG_WIRING_PROOF_UNREADABLE,
    GROSS_ERROR_FACTOR,
    HAZARD_CURRENT_A,
    PROBE_TOLERANCE,
    PROOF_MIN_RANGE_FRACTION,
    EngineError,
    GroupBoundary,
    SafetyAbortError,
    SourceFaultError,
    meter_range_floor,
    planned_source_range,
    proof_level,
    proof_note,
    readable_meter_ranges,
    run_plan,
)
from caldmm.cal.plan import (
    PlanPoint,
    TestPlan,
    generate_plan,
    point_problems,
)
from caldmm.cal.quantities import Connection, Quantity
from caldmm.cal.result import (
    RunHeader,
    load_results,
    summarize_run,
    write_results,
)
from caldmm.cal.roles import (
    Environment,
    MeterConfig,
    Point,
    Reading,
    SourceHealth,
    SourceState,
    Uncertainty,
)
from caldmm.cal.specs import (
    Traceability,
    evaluate_limit,
    load_model,
    parse_model_text,
    select_band,
    select_range,
)
from caldmm.cal.uncertainty import loading_correction, normal_coverage_factor
from caldmm.cal.verdict import (
    FLAG_CONDITIONS_NOT_MET,
    TemperatureStatus,
    Verdict,
)


# --------------------------------------------------------------------------
# Fake roles
# --------------------------------------------------------------------------

#: What a healthy, warm, recently zeroed calibrator answers.
HEALTHY = SourceHealth(operate=True, tripped=False, faults=(),
                       warm_minutes=45, days_since_cal=120, days_since_zero=3)

#: 5 ppm at 95 % -- the shape of a real ``UNCERT?``/``CAL_CONF?`` pair.
UNCERT_5PPM = Uncertainty(value=5.0, unit="PPM", confidence=0.95,
                          interval_days=365)


class FakeSource:
    """A :class:`~caldmm.cal.roles.SourceRole` that sources what it is asked.

    ``offset_ppm`` is what makes it useful: the 5700A's resistances are
    *characterized* values up to 500 ppm from nominal, so a source whose
    readback equals its request cannot show that the engine configured the
    meter from the readback.
    """

    def __init__(self, spec=None, *, offset_ppm=0.0, uncertainty=UNCERT_5PPM,
                 zero_uncertainty=None,
                 health=HEALTHY, settled=True, operate=True, log=None,
                 fail_at=None):
        self.spec = spec if spec is not None else load_model("fluke5700a")
        self.model_id = self.spec.model_id
        self.offset_ppm = float(offset_ppm)
        self.uncertainty = uncertainty
        #: What ``UNCERT?`` answers at a **zero** setpoint.  The bench unit
        #: answers the range's absolute floor in volts there
        #: (``+7.5000E-07,V,1``),
        #: not ppm of zero -- the units of the answer are open-ended
        #: ("PPM, PCT, V, A, OHM, etc.", OM 1996 lines 9055-9056).
        self.zero_uncertainty = zero_uncertainty
        self.settled = settled
        self.operate = operate
        self.log = [] if log is None else log
        self.applied = []
        self.standby_calls = 0
        self.health_calls = 0
        #: What is on the terminals right now -- a linked :class:`FakeMeter`
        #: reads *this*, not what it was configured for, because that is the
        #: only way a test can tell a pre-step reading from the point's own.
        self.last_actual = None
        #: The Thevenin impedance behind :attr:`last_actual`, from this
        #: model's own file.  On the 5700A that is 50 ohm on the dc 220 mV
        #: range and on all three ac millivolt ranges, and nothing anywhere
        #: else (``5700a-service-manual-1996.txt`` 5304 and 5371, PDF pp. 115
        #: and 118; ``5700a-operators-manual-1996.txt`` 1306-1307 and
        #: 1663-1700, PDF pp. 35 and 44).  A linked :class:`FakeMeter` divides
        #: by it: a meter double that answered the open-circuit value would be
        #: modelling an infinite input impedance while declaring 1 MOhm in its
        #: own model file, and the engine's loading correction would then read
        #: as a 50 ppm *error* instead of cancelling.
        self.last_source_impedance = None
        self._healths = list(health) if isinstance(health, list) else [health]
        self._fail_at = fail_at

    def validate(self, point):
        return []

    def apply(self, point):
        self.log.append("apply")
        self.applied.append(point)
        if self._fail_at is not None and self._fail_at(point):
            raise RuntimeError("the calibrator refused this point")
        value = float(point.value)
        actual = value * (1.0 + self.offset_ppm * 1e-6)
        self.last_actual = actual
        self.last_source_impedance = getattr(
            planned_source_range(self.spec, point), "source_impedance_ohm",
            None)
        uncertainty = self.uncertainty
        if self.zero_uncertainty is not None and value == 0.0:
            uncertainty = self.zero_uncertainty
        return SourceState(
            actual_value=actual,
            unit=Quantity(point.quantity).unit,
            frequency_hz=float(getattr(point, "frequency_hz", 0.0) or 0.0),
            range_id=getattr(point, "range_id", None),
            uncertainty=uncertainty,
            settled=self.settled,
            operate=self.operate,
        )

    def standby(self):
        self.log.append("standby")
        self.standby_calls += 1

    def health(self):
        self.log.append("health")
        index = min(self.health_calls, len(self._healths) - 1)
        self.health_calls += 1
        return self._healths[index]


class FakeMeter:
    """A :class:`~caldmm.cal.roles.MeterRole` that reads what is on the terminals.

    ``read`` returns ``what the linked source last sourced + error``, *after*
    the divider its own input impedance makes with the source's output
    impedance.  Reading the configured value instead would make a pre-step
    reading (taken at a tenth of the target, on the target's range)
    indistinguishable from the point's own, which is exactly what §9's
    pre-step exists to detect.  With no source linked it falls back to the
    configured value.
    """

    def __init__(self, spec=None, *, error=0.0, spread=1e-7, settle_s=1.0,
                 overloads=0, samples=None, log=None, fail_at=None,
                 accept_discard=True, source=None,
                 proof_reads_samples=False):
        self.spec = spec if spec is not None else load_model("hp3458a")
        self.model_id = self.spec.model_id
        self.error = float(error)
        self.spread = float(spread)
        self.settle_s = float(settle_s)
        self.overloads = int(overloads)
        self.samples = samples
        #: Whether the canned ``samples`` also answer a wiring proof -- which
        #: is what an open lead or a wrong socket does.
        self.proof_reads_samples = bool(proof_reads_samples)
        self.log = [] if log is None else log
        self.configured = []
        self.reads = []
        self.source = source
        self._fail_at = fail_at
        self._config = None
        self._value = None
        self._quantity = None
        self._range = None
        self._proof = False
        if not accept_discard:
            # A meter honouring only the MeterRole protocol's ``read(self, n)``.
            self.read = self._read_without_discard

    # -- role ------------------------------------------------------------

    def supports(self, quantity, connection=Connection.TWO_WIRE):
        function = self.spec.function(Quantity(quantity))
        if function is None:
            return False
        return (not function.connections
                or Connection(connection) in function.connections)

    def configure(self, point):
        self.log.append("configure")
        self.configured.append(point)
        if self._fail_at is not None and self._fail_at(point):
            raise RuntimeError("the meter could not be configured")
        quantity = Quantity(point.quantity)
        function = self.spec.function(quantity)
        range_arg = getattr(point, "range_arg", None)
        range_spec = None
        if range_arg is not None:
            # Exactly what MeterAdapter._select_range does with a forced range:
            # match the knob, never re-ask holds().
            for candidate in function.ranges:
                arg = (candidate.range_arg if candidate.range_arg is not None
                       else candidate.fs)
                if math.isclose(arg, float(range_arg), rel_tol=1e-9):
                    range_spec = candidate
                    break
        if range_spec is None:
            range_spec = select_range(function, abs(float(point.value)))
        if range_spec is None:
            raise RuntimeError(f"no range holds {point.value}")
        self._value = float(point.value)
        self._quantity = quantity
        self._range = range_spec
        # Canned ``samples`` are the ones a test cares about for its *point*;
        # a wiring proof at a tenth of a volt is not that point, and a fake
        # that answered 10 V to it would fail every proof in the suite.
        self._proof = str(getattr(point, "label", "")).startswith(
            "wiring proof")
        self._config = MeterConfig(
            func=function.func, range_arg=range_arg, range_fs=range_spec.fs,
            nplc=100.0, mode=getattr(point, "mode", None),
            resolution=range_spec.resolution, settle_s=self.settle_s,
            applied={"nplc": 100.0})
        return self._config

    def read(self, n, *, discard=0):
        return self._read(n, discard)

    def _read_without_discard(self, n):
        return self._read(n, 0)

    # -- internals -------------------------------------------------------

    def _loading_fraction(self) -> float:
        """``Zin/(Zin+Zs)`` -- how much of the open-circuit value reaches here.

        The physics, not the engine's policy: the divider is there whether or
        not the engine chooses to correct for it, so this stays in force above
        :data:`LOADING_MAX_FREQUENCY_HZ` too, where the row is flagged
        ``loading-uncorrected-above-100-khz`` and keeps the residual bias.

        Wideband is the one exception, and it is a real one: the 5700A's
        wideband output is 50 ohm coax whose specifications "apply to the end
        of the cable and 50 Ohm termination used for calibration"
        (``5700a-operators-manual-1996.txt`` 2528, PDF p.60), so the stated
        value is already the value at the load and there is no divider left to
        model -- which is why the engine does not correct there either.
        """
        z_source = getattr(self.source, "last_source_impedance", None)
        z_input = getattr(self._range, "input_impedance_ohm", None)
        if not z_source or not z_input:
            return 1.0
        if self._quantity is None or self._quantity is Quantity.WBV:
            return 1.0
        return float(z_input) / (float(z_input) + float(z_source))

    def _read(self, n, discard):
        self.log.append("read")
        self.reads.append((n, discard))
        if self._config is None:
            raise AssertionError("read() before configure()")
        if self.samples is not None and (self.proof_reads_samples
                                         or not self._proof):
            values = list(self.samples)
        else:
            on_terminals = getattr(self.source, "last_actual", None)
            if on_terminals is not None:
                on_terminals *= self._loading_fraction()
            base = (self._value if on_terminals is None else on_terminals) \
                + self.error
            # Symmetric about the mean, so an even n has a mean of exactly
            # ``base`` and a standard deviation of exactly ``spread``.
            values = [base + (self.spread if i % 2 == 0 else -self.spread)
                      for i in range(n)]
        if not self._proof:
            values += [9.9e37] * self.overloads
        return Reading.from_samples(
            values, overload=self.spec.is_overload, discarded=discard,
            resolution=self._config.resolution,
            unit=self._quantity.unit if self._quantity else "")


class _CalTempSource(FakeSource):
    """A source offering the optional ``cal_temperature`` capability.

    The 5700A has no live sensor: ``CAL_TEMP?`` is the ambient its stored
    constants assume, and the delta from it is what decides whether the unit
    is inside the +/-5 C window its every specification is written for.
    """

    def __init__(self, cal_temp_c=21.1, *args, error=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.cal_temp_c = cal_temp_c
        self.cal_temp_error = error
        self.cal_temp_calls = 0

    def cal_temperature(self):
        self.cal_temp_calls += 1
        if self.cal_temp_error is not None:
            raise self.cal_temp_error
        return self.cal_temp_c


class FakeProbe:
    """An :class:`~caldmm.cal.roles.EnvironmentProbe` with a fixed ambient."""

    def __init__(self, ambient_c=23.0, *, humidity_pct=45.0, source="fake",
                 error=None):
        self.ambient_c = ambient_c
        self.humidity_pct = humidity_pct
        self.source = source
        self.error = error
        self.calls = 0

    def read(self):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return Environment(ambient_c=self.ambient_c,
                           humidity_pct=self.humidity_pct,
                           timestamp=datetime(2026, 1, 1, tzinfo=timezone.utc),
                           source=self.source)


class FakeClock:
    """A monotonic clock that never sleeps."""

    def __init__(self, step_s=1.0):
        self.now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
        self.step = timedelta(seconds=step_s)

    def __call__(self):
        self.now = self.now + self.step
        return self.now


# --------------------------------------------------------------------------
# Plans
# --------------------------------------------------------------------------

def dcv_point(value=10.0, **kwargs):
    fields = dict(quantity=Quantity.DCV, value=value,
                  connection=Connection.TWO_WIRE, range_arg=10.0,
                  range_id="DC11V", interval="1y",
                  label=f"DCV {value:g} V")
    fields.update(kwargs)
    return PlanPoint(**fields)


#: A calibrator spec that **declares a temperature coefficient**.
#:
#: The shipped ``fluke5700a.yaml`` declares none -- its ``UNCERT?`` already
#: folds the tempco in -- which means no shipped model can show the engine
#: *refusing* to add one.  Parsed by the real parser so it is a real
#: :class:`~caldmm.cal.specs.ModelSpec`, not a stand-in; only the one 11 V
#: range the tests source on is defined.
TEMPCO_CALIBRATOR = parse_model_text("""
schema_version: 1
model_id: tc-calibrator
display_name: "A calibrator whose spec table states a tempco"
role: source
overload_sentinel: null
interval_basis: since_cal
intervals: [1y]
default_interval: 1y
functions:
  - quantity: DCV
    func: DCV
    connections: [2w]
    settle_s: 0.0
    ranges:
      - fs: 11.0
        range_id: DC11V
        resolution: 1e-06
        settle_s: 0.0
        limits:
          1y: {ppm_reading: 8.0, abs_floor: 4e-06}
        tempco: {ppm_reading_per_c: 0.2, ppm_range_per_c: 0.05}
""", source="<TEMPCO_CALIBRATOR>")


#: A reference meter that declares **both** a tempco and the window the
#: tempco is an adder *outside* of -- the 34401A's shape ("You must add
#: additional temperature coefficient errors ... if you are operating the
#: multimeter outside a 23 C +/- 5 C temperature range",
#: ``34401a-user.txt:9619-9624``).
WINDOWED_METER = parse_model_text("""
schema_version: 1
model_id: windowed-meter
display_name: "A meter with a tempco and the window it lies outside of"
role: meter
overload_sentinel: 9.9e37
interval_basis: since_cal
intervals: [1y]
default_interval: 1y
temperature: {spec_window_c: [18.0, 28.0], tempco_ref_c: 23.0}
functions:
  - quantity: DCV
    func: DCV
    connections: [2w]
    ranges:
      - fs: 10.0
        resolution: 1e-05
        limits:
          1y: {ppm_reading: 35.0, ppm_range: 5.0}
        tempco: {ppm_reading_per_c: 6.0, ppm_range_per_c: 1.0}
""", source="<WINDOWED_METER>")

#: A calibrator whose window is **relative** to its own stored calibration
#: temperature, as every 5700A table is ("Absolute Uncertainty +/- 5 C from
#: calibration temperature", ``5700a-operators-manual-1996.txt:1219``).
RELATIVE_WINDOW_SOURCE = parse_model_text("""
schema_version: 1
model_id: relative-window-calibrator
display_name: "A calibrator specified within +/-5 C of its calibration"
role: source
overload_sentinel: null
interval_basis: since_cal
intervals: [1y]
default_interval: 1y
temperature: {relative_to: cal_temp, half_width_c: 5.0}
functions:
  - quantity: DCV
    func: DCV
    connections: [2w]
    settle_s: 0.0
    ranges:
      - fs: 11.0
        range_id: DC11V
        resolution: 1e-06
        settle_s: 0.0
        limits:
          1y: {ppm_reading: 8.0, abs_floor: 4e-06}
""", source="<RELATIVE_WINDOW_SOURCE>")


#: A calibrator whose tables state the confidence level they hold at, as the
#: 5700A's do ("Fluke guarantees performance verification using specifications
#: stated to 99% confidence level", ``5700a-operators-manual-1996.txt:1214``).
STATED_CONFIDENCE_SOURCE = parse_model_text("""
schema_version: 1
model_id: stated-confidence-calibrator
display_name: "A calibrator whose tables state 99 % confidence"
role: source
overload_sentinel: null
interval_basis: since_cal
intervals: [1y]
default_interval: 1y
spec_confidence: 0.99
functions:
  - quantity: DCV
    func: DCV
    connections: [2w]
    settle_s: 0.0
    ranges:
      - fs: 11.0
        range_id: DC11V
        resolution: 1e-06
        settle_s: 0.0
        limits:
          1y: {ppm_reading: 8.0, abs_floor: 4e-06}
""", source="<STATED_CONFIDENCE_SOURCE>")

#: A reference meter that states both a confidence level and the traceability
#: of the standards its figures are *relative to* -- the 3458A's shape ("All
#: 3458A accuracy specifications are relative to the calibration standards ...
#: For DCV, 2 ppm is the traceability error from the factory",
#: ``3458a-datasheet.txt:282-287``).
TRACEABLE_METER = parse_model_text("""
schema_version: 1
model_id: traceable-meter
display_name: "A meter stating its confidence and its traceability"
role: meter
overload_sentinel: 9.9e37
interval_basis: since_cal
intervals: [1y]
default_interval: 1y
spec_confidence: 0.95
functions:
  - quantity: DCV
    func: DCV
    connections: [2w]
    traceability: {ppm_reading: 2.0, confidence: 0.99, note: "factory to NIST"}
    ranges:
      - fs: 10.0
        resolution: 1e-05
        limits:
          1y: {ppm_reading: 35.0, ppm_range: 5.0}
""", source="<TRACEABLE_METER>")


#: A meter whose ranges declare **no** overrange, so a readback a hair above
#: the nominal really does belong on the next range up.  Every shipped meter
#: specifies an overrange (20 % on the HPs, 100 % on the 1281), which is what
#: makes the re-target path invisible against a real model file.
NO_OVERRANGE_METER = parse_model_text("""
schema_version: 1
model_id: no-overrange-meter
display_name: "A meter with no overrange anywhere"
role: meter
overload_sentinel: 9.9e37
interval_basis: since_cal
intervals: [1y]
default_interval: 1y
functions:
  - quantity: DCV
    func: DCV
    connections: [2w]
    ranges:
      - fs: 10.0
        resolution: 1e-05
        limits:
          1y: {ppm_reading: 35.0, ppm_range: 5.0}
      - fs: 100.0
        resolution: 1e-04
        limits:
          1y: {ppm_reading: 35.0, ppm_range: 5.0}
""", source="<NO_OVERRANGE_METER>")


#: A meter that can read the 5700A's **wideband** output.  No shipped model
#: declares ``WBV``, so without this the one output whose 50 ohm is inside its
#: own specification -- "Specifications apply to the end of the cable and 50 Ohm
#: termination used for calibration" (``5700a-operators-manual-1996.txt`` 2528,
#: PDF p.60) -- can never reach the loading correction to be excluded from it.
WIDEBAND_METER = parse_model_text("""
schema_version: 1
model_id: wideband-meter
display_name: "A meter with a wideband ac function"
role: meter
overload_sentinel: 9.9e37
interval_basis: since_cal
intervals: [1y]
default_interval: 1y
functions:
  - quantity: WBV
    func: ACV
    connections: [2w]
    settle_s: 0.0
    ranges:
      - fs: 1.1
        resolution: 1e-06
        input_impedance_ohm: 1.0e+6
        settle_s: 0.0
        bands:
          - freq_hz: [10.0, 1000000.0]
            limits:
              1y: {ppm_reading: 5000.0, ppm_range: 500.0}
""", source="<WIDEBAND_METER>")


def make_plan(points=None, *, dut_role="meter", meter_model="hp3458a",
              **kwargs):
    fields = dict(dut_role=dut_role, source_model_id="fluke5700a",
                  meter_model_id=meter_model,
                  points=tuple(points if points is not None else [dcv_point()]),
                  interval="1y", samples=4, discards=0)
    fields.update(kwargs)
    return TestPlan(**fields)


def run(plan, source=None, meter=None, **kwargs):
    """Drive a plan to completion with a probe at 23 C and no real sleeping."""
    source = source if source is not None else FakeSource()
    meter = meter if meter is not None else FakeMeter()
    if getattr(meter, "source", None) is None:
        meter.source = source              # the meter reads the terminals
    kwargs.setdefault("environment", FakeProbe())
    kwargs.setdefault("sleep", lambda seconds: None)
    kwargs.setdefault("clock", FakeClock())
    return list(run_plan(plan, source, meter, **kwargs))


# --------------------------------------------------------------------------
# The per-point sequence
# --------------------------------------------------------------------------

class TestPointSequence:
    """Health, configure-from-the-nominal, apply, settle, read."""

    def test_calls_are_in_the_order_the_physics_requires(self):
        """The settle is *between* configure and read, not anywhere else.

        Its position is the whole of it: sleeping after the samples are taken
        is not settling, and a run that reads a 5700A 3 s early records the
        output on its way to the setpoint as though it were the setpoint.

        ``configure`` comes **before** ``apply``: the meter must be on the
        function and range this point needs before the output is energized
        into it (H03).  The second ``health`` is after the reading: a trip
        between ``apply`` and ``read`` is a trip *this* point's samples were
        taken through (M81).

        The proof's ``configure`` has a ``standby`` in front of it: the range
        that holds an 11 mV proof is below the range that holds the point, and
        asking for it with the previous point still live is the hazard §9
        orders these calls to prevent (:class:`TestSafeSequencing`).
        """
        log = []
        source = FakeSource(log=log)
        meter = FakeMeter(log=log)
        run(make_plan(), source, meter,
            sleep=lambda seconds: log.append("settle"))
        # standby at the group boundary, the wiring proof at a safe level
        # (§8) with its own standby in front of it, then the point -- which
        # ranges *up* under the proof level and needs none -- then standby.
        assert log == ["standby", "health",
                       "standby", "configure", "apply", "settle", "read",
                       "configure", "apply", "settle", "read", "health",
                       "standby"]

    def test_meter_is_configured_from_the_nominal_before_the_source_operates(self):
        """H03: the range comes from the plan, not from a live output.

        500 ppm off nominal is what a characterized 5700A resistance does, and
        the row still records the readback as the reference -- but the *range*
        was chosen before anything was energized.
        """
        source = FakeSource(offset_ppm=500.0)
        meter = FakeMeter()
        rows = run(make_plan(), source, meter)
        # [0] is the wiring proof's own configure (§8); the point's is [1].
        assert meter.configured[1].value == pytest.approx(10.0)
        # One configure for the point: the readback stays on the range the
        # nominal chose.
        assert len(meter.configured) == 2
        # ... and the row still records the nominal that was asked for, and
        # the readback as the reference.
        assert rows[0].nominal == 10.0
        assert rows[0].source_value == pytest.approx(10.005)

    def test_the_meter_is_reconfigured_when_the_readback_changes_the_range(self):
        """The one case the readback still governs (M04's other half).

        A characterized value that its nominal's range cannot hold -- here a
        toy meter whose ranges declare no overrange at all -- is re-targeted
        after the apply.  Where the range *would not* change, the meter is
        left alone: re-sending a range mid-point costs a settle and buys
        nothing.
        """
        point = dcv_point(range_arg=None)
        source = FakeSource(offset_ppm=1000.0)          # 10.01 V, off a 10 V fs
        meter = FakeMeter(NO_OVERRANGE_METER)
        rows = run(make_plan([point], meter_model="no-overrange-meter"),
                   source, meter)
        assert [p.value for p in meter.configured[1:]] == [
            pytest.approx(10.0), pytest.approx(10.01)]
        assert rows[0].meter_range_fs == pytest.approx(100.0)

    def test_retargeted_point_keeps_the_plan_fields(self):
        point = dcv_point(samples=6, discards=2, note="spec ratio 3")
        source = FakeSource(offset_ppm=500.0)
        meter = FakeMeter()
        run(make_plan([point]), source, meter)
        configured = meter.configured[-1]
        assert isinstance(configured, PlanPoint)
        assert (configured.samples, configured.discards) == (6, 2)
        assert configured.note == "spec ratio 3"
        assert configured.range_arg == 10.0

    def test_a_characterized_readback_is_judged_not_refused(self):
        """The trap: ``RangeSpec.holds`` has no overrange allowance.

        A 3458A pinned to its 10 ohm range reading a characterized 10.005 ohm
        is entirely ordinary -- the hardware has ~20 % of headroom -- but the
        offline validator, which is right to be strict about a *plan*, calls it
        off range.  Re-validating the point the engine hands the meter would
        fail 7 of the 17 resistance points of every meter-DUT run.
        """
        point = PlanPoint(quantity=Quantity.RES, value=10.0,
                          connection=Connection.FOUR_WIRE, range_id="OHM10",
                          range_arg=10.0, interval="1y", label="RES 10 OHM")
        plan = make_plan([point], meter_model="hp3458a")
        meter = FakeMeter(load_model("hp3458a"))
        rows = run(plan, FakeSource(offset_ppm=500.0), meter)

        # The trap is real: the same point, re-targeted, does not validate.
        problems = point_problems(replace(point, value=10.005),
                                  load_model("fluke5700a"),
                                  load_model("hp3458a"),
                                  dut_role="meter", interval="1y")
        assert any("does not read" in p for p in problems)

        # The engine ran it anyway, and judged it -- on the range the plan
        # pinned, which the characterized value does not move it off.
        assert FLAG_POINT_FAILED not in rows[0].flags
        assert meter.configured[-1].value == pytest.approx(10.0)
        assert rows[0].source_value == pytest.approx(10.005)
        assert rows[0].tolerance_limit == pytest.approx(15e-6 * 10.005
                                                        + 5e-6 * 10.0)

    def test_samples_and_discards_come_from_the_plan(self):
        plan = make_plan([dcv_point(samples=7, discards=3)])
        meter = FakeMeter()
        rows = run(plan, FakeSource(), meter)
        # The wiring proof takes a single quick reading of its own first --
        # and **discards what the plan says to discard**.  It is taken
        # immediately after a re-configure and a source step, which is exactly
        # the sample the plan throws away; averaging it in would fail a
        # +/-10 % check and abandon a run for a stale reading.
        assert meter.reads == [(1, 3), (7, 3)]
        assert rows[0].n == 7
        assert rows[0].discarded == 3

    def test_a_meter_without_discard_support_is_refused_before_energizing(self):
        plan = make_plan([dcv_point(discards=1)])
        source = FakeSource()
        meter = FakeMeter(accept_discard=False)
        with pytest.raises(EngineError, match="discard"):
            run_plan(plan, source, meter, environment=FakeProbe())
        assert source.standby_calls == 0    # nothing was ever energized

    def test_a_meter_without_discard_support_is_fine_with_no_discards(self):
        plan = make_plan([dcv_point(discards=0)])
        rows = run(plan, FakeSource(), FakeMeter(accept_discard=False))
        assert rows[0].n == 4


class _RangingMeter(FakeMeter):
    """A meter with the manual-ranging capability the engine verifies.

    ``range_readback`` is what the instrument answers, which is not the same
    thing as what the adapter *believes* it applied -- proving they agree
    before an output above 22 V is energized is the whole point of the check.
    """

    def __init__(self, *args, supports_manual_range=True, readback="truthful",
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.supports_manual_range = supports_manual_range
        self._readback = readback
        self.readback_calls = 0

    def range_readback(self):
        self.readback_calls += 1
        if self._readback == "truthful":
            return None if self._config is None else self._config.range_fs
        if self._readback == "silent":
            return None
        if isinstance(self._readback, Exception):
            raise self._readback
        return float(self._readback)


class _WiredSource(FakeSource):
    """A source that records the meter's range at the moment it energizes.

    The H03 assertion cannot be made from the call log alone: "configure came
    before apply" says nothing about *which* range the meter was left on when
    the output came up.
    """

    def __init__(self, meter, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.meter = meter
        self.range_at_apply = []

    def apply(self, point):
        config = getattr(self.meter, "_config", None)
        self.range_at_apply.append(None if config is None else config.range_fs)
        return super().apply(point)


def dci_point(value, **kwargs):
    fields = dict(quantity=Quantity.DCI, value=value,
                  connection=Connection.TWO_WIRE, interval="1y",
                  label=f"DCI {value:g} A")
    fields.update(kwargs)
    return PlanPoint(**fields)


class TestManualRanging:
    """Global Constraints §9: a fixed range from the nominal, verified.

    The failure this exists to prevent is H03: every dc-current range of a
    generated source-DUT plan starts at 0 A, which selects the 3458A's 100 nA
    range (a 545 kohm shunt), and the 10 % point that follows was sourced into
    it -- 12 V demanded against a 10 V compliance limit, tripping the
    calibrator on every range.
    """

    def test_a_post_zero_current_point_is_never_read_on_the_100_na_range(self):
        points = [dci_point(0.0), dci_point(22e-6)]
        plan = make_plan(points, dut_role="source", meter_model="hp3458a")
        meter = FakeMeter(load_model("hp3458a"))
        source = _WiredSource(meter)
        run(plan, source, meter)
        # [0] is the wiring proof's own 100 uA.  The zero then selects the
        # smallest range, as it should -- and the point after it is on a range
        # that holds 22 uA before the output comes up.
        assert source.range_at_apply[1] == pytest.approx(1e-7)
        assert source.range_at_apply[2] >= 22e-6

    def test_a_hazardous_point_is_refused_when_the_meter_disagrees(self):
        point = PlanPoint(quantity=Quantity.DCV, value=100.0,
                          connection=Connection.TWO_WIRE, range_id="DC220V",
                          interval="1y", label="DCV 100 V")
        meter = _RangingMeter(readback=10.0)          # says 10 V, applied 100 V
        source = FakeSource()
        generator = run_plan(make_plan([point]), source, meter,
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        rows = []
        with pytest.raises(SafetyAbortError, match="range"):
            for row in generator:
                rows.append(row)
        assert len(rows) == 1
        assert FLAG_METER_RANGE_MISMATCH in rows[0].flags
        assert FLAG_POINT_FAILED in rows[0].flags
        assert rows[0].verdict is Verdict.NO_SPEC
        # The safe wiring proof happened; the 100 V point never did.
        assert [p.value for p in source.applied] == [pytest.approx(0.22)]
        assert source.log[-1] == "standby"

    def test_a_hazardous_point_runs_when_the_readback_agrees(self):
        point = PlanPoint(quantity=Quantity.DCV, value=100.0,
                          connection=Connection.TWO_WIRE, range_id="DC220V",
                          interval="1y", label="DCV 100 V")
        meter = _RangingMeter()
        rows = run(make_plan([point]), FakeSource(), meter)
        assert meter.readback_calls == 1
        assert FLAG_METER_RANGE_MISMATCH not in rows[0].flags

    def test_a_meter_that_cannot_report_its_range_is_not_blocked(self):
        """``None`` is "I cannot say", which is not "the range is wrong"."""
        point = PlanPoint(quantity=Quantity.DCV, value=100.0,
                          connection=Connection.TWO_WIRE, range_id="DC220V",
                          interval="1y", label="DCV 100 V")
        meter = _RangingMeter(readback="silent")
        rows = run(make_plan([point]), FakeSource(), meter)
        assert FLAG_METER_RANGE_MISMATCH not in rows[0].flags
        assert FLAG_POINT_FAILED not in rows[0].flags

    def test_a_point_below_the_thresholds_is_not_read_back(self):
        meter = _RangingMeter()
        run(make_plan(), FakeSource(), meter)         # 10 V on a 10 V range
        assert meter.readback_calls == 0

    def test_every_current_point_above_220_ma_pre_steps(self):
        """Manual ranging does not waive it: a wrong shunt at 1 A is the risk."""
        point = dci_point(1.0, range_id="DC2_2A")
        plan = make_plan([point], dut_role="source", meter_model="hp3458a")
        meter = _RangingMeter(load_model("hp3458a"))
        source = FakeSource()
        rows = run(plan, source, meter)
        # The wiring proof's 2.2 mA, then the pre-step's 100 mA, then the amp.
        assert [p.value for p in source.applied] == [pytest.approx(2.2e-3),
                                                     pytest.approx(0.1),
                                                     pytest.approx(1.0)]
        # Configured from the nominal for the point, from the nominal again
        # for the (fast) pre-step probe -- so it is on the point's own range
        # -- and again after it: the meter is back on the full value's range,
        # with the plan's knobs, before the source steps up.
        assert [p.value for p in meter.configured[1:]] == [pytest.approx(1.0),
                                                           pytest.approx(1.0),
                                                           pytest.approx(1.0)]
        assert FLAG_PRESTEP_FAILED not in rows[0].flags

    def test_a_voltage_point_pre_steps_only_when_the_meter_cannot_fix_its_range(self):
        point = PlanPoint(quantity=Quantity.DCV, value=100.0,
                          connection=Connection.TWO_WIRE, range_id="DC220V",
                          interval="1y", label="DCV 100 V")
        fixed = FakeSource()
        run(make_plan([point]), fixed, _RangingMeter())
        # The wiring proof's 220 mV, then the point.  No pre-step.
        assert [p.value for p in fixed.applied] == [pytest.approx(0.22),
                                                    pytest.approx(100.0)]

        auto = FakeSource()
        run(make_plan([point]), auto,
            _RangingMeter(supports_manual_range=False))
        assert [p.value for p in auto.applied] == [pytest.approx(0.22),
                                                   pytest.approx(10.0),
                                                   pytest.approx(100.0)]

    def _1281_points(self):
        return [PlanPoint(quantity=Quantity.DCV, value=100.0,
                          connection=Connection.TWO_WIRE, range_id="DC220V",
                          range_arg=100.0, interval="1y", label="DCV 100 V"),
                PlanPoint(quantity=Quantity.DCV, value=1000.0,
                          connection=Connection.TWO_WIRE, range_id="DC1100V",
                          range_arg=1000.0, interval="1y", label="DCV 1000 V")]


    def test_the_range_is_verified_again_after_the_pre_step_reconfigures(self):
        """The pre-step re-configures the meter; the read-back must follow it.

        Otherwise the configuration energised against is not the one that was
        read back -- the check would be answering about a range the meter has
        since been taken off.
        """
        point = dci_point(1.0, range_id="DC2_2A")
        plan = make_plan([point], dut_role="source", meter_model="hp3458a")
        meter = _RangingMeter(load_model("hp3458a"))
        run(plan, FakeSource(), meter)
        assert meter.readback_calls == 2

    def test_a_failed_pre_step_aborts_the_run_before_the_full_value(self):
        point = dci_point(1.0, range_id="DC2_2A")
        plan = make_plan([point], dut_role="source", meter_model="hp3458a")
        # Reads 0 A whatever is sourced: an open lead, or the wrong terminal.
        meter = FakeMeter(load_model("hp3458a"), samples=[0.0])
        source = FakeSource()
        generator = run_plan(plan, source, meter, environment=FakeProbe(),
                             sleep=lambda s: None, clock=FakeClock())
        rows = []
        with pytest.raises(SafetyAbortError, match="pre-step"):
            for row in generator:
                rows.append(row)
        assert len(rows) == 1
        assert FLAG_PRESTEP_FAILED in rows[0].flags
        # The proof's 2.2 mA passed; 1 A was never sourced into an open lead.
        assert [p.value for p in source.applied] == [pytest.approx(2.2e-3),
                                                     pytest.approx(0.1)]
        assert source.log[-1] == "standby"


class _ErrorAtMeter(FakeMeter):
    """A meter that misreads only when configured for one particular value.

    A run-wide offset would be caught by the wiring proof at 11 mV, which is
    the proof doing its job -- but it makes it impossible to test what happens
    when a *later* point goes wrong.
    """

    def __init__(self, *args, at_value=None, gross=0.0, **kwargs):
        super().__init__(*args, **kwargs)
        self.at_value = at_value
        self.gross = float(gross)

    def configure(self, point):
        config = super().configure(point)
        self.error = (self.gross if self.at_value is not None
                      and math.isclose(float(point.value), self.at_value,
                                       rel_tol=1e-9, abs_tol=0.0)
                      else 0.0)
        return config


class _UnreadableProofMeter(FakeMeter):
    """A meter that *raises* on the proof read instead of answering it.

    The real one was an HP 3458A whose driver timed out routing a 4-wire
    resistance point into the burst path; anything that loses the interface
    mid-proof looks the same from here.  The point is that no value came
    back at all, which is a different fact from a value that disagrees.
    """

    def read(self, n, *, discard=0):
        if self._proof:
            raise TimeoutError(
                "3458A burst: 0 in MEM + 1 leaked != 2 requested after 22.9 s")
        return super().read(n, discard=discard)


class TestWiringProof:
    """Global Constraints §8: prove the wiring before trusting it.

    A small safe output and a loose check, before the first point, at every
    wiring group, on every resume and whenever the source changes range.  A
    lead in the wrong socket reads as nothing at 10 mV instead of as a
    catastrophe at 1000 V twenty minutes later.
    """

    def test_the_first_point_is_preceded_by_a_proof_at_a_safe_level(self):
        source = FakeSource()
        rows = run(make_plan(), source, FakeMeter())
        # max(10 mV, 0.1 % of the 11 V range) = 11 mV.
        assert [p.value for p in source.applied] == [pytest.approx(0.011),
                                                     pytest.approx(10.0)]
        assert len(rows) == 1                       # a proof that passed is
        assert FLAG_WIRING_PROOF_FAILED not in rows[0].flags   # not a row

    def test_a_meter_reading_nothing_aborts_before_the_first_real_point(self):
        source = FakeSource()
        # ``samples`` answer the proof too, which is what an open lead does.
        meter = FakeMeter(samples=[0.0], proof_reads_samples=True)
        generator = run_plan(make_plan(), source, meter,
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        rows = []
        with pytest.raises(SafetyAbortError, match="wiring proof"):
            for row in generator:
                rows.append(row)
        assert len(rows) == 1
        assert FLAG_WIRING_PROOF_FAILED in rows[0].flags
        assert FLAG_POINT_FAILED in rows[0].flags
        assert rows[0].verdict is Verdict.NO_SPEC
        assert "wiring proof" in rows[0].label
        # 10 V was never sourced into wiring that could not prove itself.
        assert [p.value for p in source.applied] == [pytest.approx(0.011)]
        assert source.log[-1] == "standby"
        # ... and *that* is the case where the wiring may be named.
        assert "the wiring is not what this plan assumes" in rows[0].reason

    def test_a_proof_that_cannot_be_read_is_not_a_wiring_fault(self):
        """A read that raised is not a read that disagreed.

        A 4-wire resistance run was abandoned twice on a correctly wired
        bench because a driver ``TimeoutError`` arrived as
        ``wiring-proof-failed``, whose whole meaning is a statement about
        the leads.  The meter returned no value, so there is nothing to
        compare and nothing to say about the wiring -- only what broke.
        """
        source = FakeSource()
        generator = run_plan(make_plan(), source, _UnreadableProofMeter(),
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        rows = []
        with pytest.raises(SafetyAbortError) as excinfo:
            for row in generator:
                rows.append(row)

        assert len(rows) == 1
        assert FLAG_WIRING_PROOF_UNREADABLE in rows[0].flags
        assert FLAG_WIRING_PROOF_FAILED not in rows[0].flags
        assert FLAG_POINT_FAILED in rows[0].flags
        assert excinfo.value.flag == FLAG_WIRING_PROOF_UNREADABLE

        message = str(excinfo.value)
        # It names what actually went wrong ...
        assert "TimeoutError" in message
        assert "0 in MEM" in message
        # ... and claims nothing whatever about the leads.
        assert "the wiring is not what this plan assumes" not in message
        assert "says nothing about the wiring" in message
        assert message == rows[0].reason

        # The abort is still an abort: the planned level was never sourced
        # and the output is off.
        assert [p.value for p in source.applied] == [pytest.approx(0.011)]
        assert source.log[-1] == "standby"

    def test_a_source_range_change_inside_a_group_triggers_another_proof(self):
        points = [dcv_point(value=1.0, range_id="DC2_2V"),
                  dcv_point(value=2.0, range_id="DC2_2V"),
                  dcv_point(value=10.0, range_id="DC11V")]
        source = FakeSource()
        rows = run(make_plan(points), source, FakeMeter())
        assert len(rows) == 3
        # One proof at the group start, one when the range changes, and none
        # for the plain point in between.
        assert [p.value for p in source.applied] == [
            pytest.approx(0.01), pytest.approx(1.0), pytest.approx(2.0),
            pytest.approx(0.011), pytest.approx(10.0)]

    def test_every_group_and_every_resume_is_proved(self):
        source = FakeSource()
        run(three_group_plan(), source)
        proofs = [p for p in source.applied
                  if str(p.label).startswith("wiring proof")]
        assert [p.label.split(" before ")[0] for p in proofs] == [
            "wiring proof (run start)", "wiring proof (group start)",
            "wiring proof (group start)"]

        resumed = FakeSource()
        run(three_group_plan(), resumed, start_group=1)
        assert len([p for p in resumed.applied
                    if str(p.label).startswith("wiring proof")]) == 2

    def test_the_ohms_short_cannot_prove_anything_so_the_next_point_does(self):
        """A short reads near zero with the leads in the air.

        The generated resistance group opens with it, so a proof "taken" there
        would prove nothing and -- worse -- would count as the group's proof.
        """
        short = PlanPoint(quantity=Quantity.RES, value=0.0,
                          connection=Connection.FOUR_WIRE, range_id="OHM0",
                          interval="1y", label="RES short")
        standard = PlanPoint(quantity=Quantity.RES, value=10.0,
                             connection=Connection.FOUR_WIRE,
                             range_id="OHM10", range_arg=10.0,
                             interval="1y", label="RES 10 OHM")
        source = FakeSource()
        rows = run(make_plan([short, standard], meter_model="hp3458a"), source,
                   FakeMeter(load_model("hp3458a")))
        assert len(rows) == 2
        assert [p.value for p in source.applied] == [
            pytest.approx(0.0),        # the short, unproven and unprovable
            pytest.approx(10.0),       # the proof, at the next standard
            pytest.approx(10.0)]       # ... and then the point itself
        assert str(source.applied[1].label).startswith("wiring proof")

    def test_a_resistance_point_is_proved_against_the_standard_itself(self):
        point = PlanPoint(quantity=Quantity.RES, value=10.0,
                          connection=Connection.FOUR_WIRE, range_id="OHM10",
                          range_arg=10.0, interval="1y", label="RES 10 OHM")
        source = FakeSource()
        run(make_plan([point], meter_model="hp3458a"), source,
            FakeMeter(load_model("hp3458a")))
        assert [p.value for p in source.applied] == [pytest.approx(10.0),
                                                     pytest.approx(10.0)]


class _TimelineMeter(FakeMeter):
    """A meter that writes the *range* it was set to into the shared log.

    ``FakeMeter`` logs a bare ``configure``, which cannot answer the question
    §9 is about: not "did configure come before apply" but "which range was
    the meter on when the output came up".  The range's full scale replaces
    that entry, so one interleaved timeline carries standbys, range commands
    and source amplitudes together.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._label = ""
        #: ``(label, quantity, level, range_fs)`` per reading -- what was on
        #: the terminals and the range it was read on.
        self.read_on = []

    def configure(self, point):
        config = super().configure(point)
        self._label = str(getattr(point, "label", ""))
        if self.log and self.log[-1] == "configure":
            self.log[-1] = f"range {config.range_fs:g}"
        return config

    def range_readback(self):
        """The 34401A this fake stands in for answers ``<func>:RANGe?``.

        Modelled because §9's pre-step now depends on it: a meter whose range
        cannot be **read back** gets the 10 % pre-step above 22 V, exactly as
        one that cannot fix its range at all, and a fake without the
        capability would put pre-steps into these timelines that a real
        34401A run does not have.
        """
        return None if self._config is None else self._config.range_fs

    def _read(self, n, discard):
        self.read_on.append((self._label, self._quantity, self._value,
                             self._config.range_fs))
        return super()._read(n, discard)


class _TimelineSource(FakeSource):
    """A source that writes the level it energized into the shared log."""

    def apply(self, point):
        state = super().apply(point)
        if self.log and self.log[-1] == "apply":
            self.log[-1] = f"apply {float(point.value):g}"
        return state


def ranged_below_live(log):
    """Every moment the meter was put on a range below the live output.

    Walks the interleaved timeline keeping track of what is on the terminals:
    an ``apply`` energizes it, a ``standby`` takes it off.  Returns the
    ``(live, range_fs)`` pairs it found -- H03's hazard, and the whole of
    Global Constraints §9's ordering rule is that this list is empty.
    """
    live = 0.0
    hazards = []
    for entry in log:
        if entry.startswith("apply "):
            live = abs(float(entry.split()[1]))
        elif entry == "standby":
            live = 0.0
        elif entry.startswith("range "):
            fs = abs(float(entry.split()[1]))
            if fs < live:
                hazards.append((live, fs))
    return hazards


class TestSafeSequencing:
    """§9 as one timeline: standby, meter range up, then source amplitude up.

    Nothing between two points of a group de-energizes on its own -- the
    engine's :func:`_de_energize` runs at group boundaries and on the way out
    -- so a wiring proof, which is by definition a step *down*, re-ranged the
    meter with the **previous** point still on the terminals.  On a 34401A
    dc-current group the 0.1 A -> 1 A step sent ``CURR:DC:RANG 1E-02``, the
    10 mA range and its 5 ohm shunt, while the calibrator was still sourcing
    100 mA -- ten times that range's rating -- and one point later the same
    mistake meets 1 A: 5 V demanded against the 2 V compliance limit for
    1-2.2 A, fault 1819 and a trip in the middle of a group.

    The rule is: previous point live -> **standby** -> meter to the range that
    holds the probe -> probe -> meter **up** to the point's own range (under
    the harmless probe level) -> the planned value.
    """

    def _current_group(self, *values):
        return [dci_point(v, range_id=("DC220MA" if abs(v) <= 0.22
                                       else "DC2_2A")) for v in values]


    def test_a_standby_immediately_precedes_the_range_command_of_a_probe(self):
        """The two halves are one rule: the probe's range is a step *down*."""
        log = []
        run(make_plan(self._current_group(0.1, 1.0), dut_role="source"),
            _TimelineSource(log=log), _TimelineMeter(log=log))
        probe = log.index("apply 0.0022")
        assert log[probe - 2:probe] == ["standby", "range 0.01"]


    def test_the_nominal_range_is_set_before_the_planned_value_is_applied(self):
        """§9's other half, and the reason the probe's range may be low: the
        meter steps **up** while only the probe level is live."""
        log = []
        run(make_plan(self._current_group(0.1, 1.0), dut_role="source"),
            _TimelineSource(log=log), _TimelineMeter(log=log))
        assert log.index("range 1") < log.index("apply 1")
        # ... and the step up happened with the probe, not the point, live.
        assert log[log.index("range 1") - 1] == "read"

    def test_a_plain_point_inside_the_same_source_range_is_not_re_proved(self):
        log = []
        source = _TimelineSource(log=log)
        run(make_plan(self._current_group(0.1, 1.0, 2.0), dut_role="source"),
            source, _TimelineMeter(log=log))
        proofs = [p for p in source.applied
                  if str(p.label).startswith("wiring proof")]
        assert len(proofs) == 2                  # the group start, and 1 A
        assert ranged_below_live(log) == []


    def test_a_descending_point_de_energizes_before_the_meter_steps_down(self):
        """No proof is due here -- both points are on one source range -- so
        the ordinary path owns the standby."""
        points = [PlanPoint(quantity=Quantity.DCV, value=200.0,
                            connection=Connection.TWO_WIRE, range_id="DC220V",
                            interval="1y", label="DCV 200 V"),
                  PlanPoint(quantity=Quantity.DCV, value=30.0,
                            connection=Connection.TWO_WIRE, range_id="DC220V",
                            interval="1y", label="DCV 30 V")]
        log = []
        source = _TimelineSource(log=log)
        run(make_plan(points, dut_role="source"), source,
            _TimelineMeter(log=log))
        assert [p.value for p in source.applied
                if not str(p.label).startswith("wiring proof")] == [
                    pytest.approx(200.0), pytest.approx(30.0)]
        assert ranged_below_live(log) == []
        assert log[log.index("range 100") - 1] == "standby"

    def test_a_standby_measure_could_not_confirm_names_itself_on_the_row(self):
        """The other standby: the ordinary point path, no proof in the way.

        It ends the run as a `point-failed` like any other exception unless
        the rule names itself, and this is the one whose consequence is a
        possibly live output -- so `cal_analyze` has to be able to lead with
        it without parsing the message.
        """
        points = [PlanPoint(quantity=Quantity.DCV, value=200.0,
                            connection=Connection.TWO_WIRE, range_id="DC220V",
                            interval="1y", label="DCV 200 V"),
                  PlanPoint(quantity=Quantity.DCV, value=30.0,
                            connection=Connection.TWO_WIRE, range_id="DC220V",
                            interval="1y", label="DCV 30 V")]

        class _StuckAtTwoHundred(_TimelineSource):
            """A calibrator that will not come off 200 V."""

            def standby(self):
                stuck = self.last_actual == 200.0
                super().standby()
                if stuck:
                    raise RuntimeError("STBY sent but the OPER bit is still set")

        log = []
        source = _StuckAtTwoHundred(log=log)
        generator = run_plan(make_plan(points, dut_role="source"), source,
                             _TimelineMeter(log=log),
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        rows = []
        with pytest.raises(EngineError, match="MAY STILL BE LIVE"):
            for row in generator:
                rows.append(row)
        assert rows[-1].label == "DCV 30 V"          # not the proof's row
        assert FLAG_POINT_FAILED in rows[-1].flags
        assert FLAG_STANDBY_NOT_CONFIRMED in rows[-1].flags
        assert "range 100" not in log                # never stepped the meter
        assert [p.value for p in source.applied
                if not str(p.label).startswith("wiring proof")] == [
                    pytest.approx(200.0)]

    def test_a_source_that_will_not_confirm_standby_stops_the_run(self):
        """M47: the adapter verifies STBY, and a meter must not be re-ranged
        on an output that may still be live."""
        class _ObstinateAfterTheFirst(_TimelineSource):
            def standby(self):
                super().standby()
                if self.standby_calls > 1:
                    raise RuntimeError("STBY sent but the OPER bit is still set")

        log = []
        source = _ObstinateAfterTheFirst(log=log)
        generator = run_plan(make_plan(self._current_group(0.1, 1.0),
                                       dut_role="source"),
                             source, _TimelineMeter(log=log),
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        rows = []
        with pytest.raises(EngineError, match="MAY STILL BE LIVE"):
            for row in generator:
                rows.append(row)
        assert rows and FLAG_POINT_FAILED in rows[-1].flags
        # ... and the row names the rule, not just "point-failed": this is the
        # one abort whose consequence is an output that may still be live, and
        # `cal_analyze` has to be able to say so without parsing a message.
        assert FLAG_STANDBY_NOT_CONFIRMED in rows[-1].flags
        # The meter was never re-ranged for the probe it could not de-energize
        # for, and nothing was sourced after it.
        assert "range 0.01" not in log
        assert not [entry for entry in log if entry.startswith("apply ")]


def proof_read_range(spec, quantity, level, frequency_hz=0.0):
    """The meter range a proof of *level* is read on -- the smallest that holds it.

    The rule every meter adapter selects by, with the banded (ac) ranges
    filtered by the tone: a range whose table does not reach the frequency is
    not a range the reading can be judged on.
    """
    function = spec.function(quantity)
    if quantity.unit == "OHM":
        return select_range(function, abs(level), for_meter_config=True)
    for range_spec in readable_meter_ranges(function, frequency_hz):
        if range_spec.holds(abs(level), allow_overrange=True):
            return range_spec
    return None


class TestProofLevelIsResolvableOnTheMeter:
    """The controller's amendment of §8, 2026-08-22, on the code path.

    The level is still ``max(10 mV, 0.1 % of the *source* range)`` wherever the
    meter can read that; where it cannot, it is raised to what the meter can,
    and where nothing safe is readable the proof is skipped and said so.
    """


    def test_a_meter_that_can_already_read_it_keeps_the_section_8_level(self):
        """The amendment is a floor, not a replacement: the 3458A is untouched."""
        source_spec = load_model("fluke5700a")
        meter_spec = load_model("hp3458a")
        for point in (Point(quantity=Quantity.DCV, value=1000.0,
                            connection=Connection.TWO_WIRE,
                            range_id="DC1100V"),
                      Point(quantity=Quantity.DCI, value=1.0,
                            connection=Connection.TWO_WIRE,
                            range_id="DC2_2A")):
            assert (proof_level(source_spec, point, meter_spec)
                    == pytest.approx(proof_level(source_spec, point)))


    UNREADABLE = textwrap.dedent("""\
        schema_version: 1
        model_id: toy-ammeter
        display_name: "A meter with one coarse current range"
        role: meter
        overload_sentinel: 9.9e+37
        interval_basis: since_cal
        intervals: [1y]
        default_interval: 1y
        functions:
          - quantity: DCI
            func: DCI
            ranges:
              - fs: 3.0
                min_reading_frac: 0.1
                limits: {1y: {pct_reading: 0.1, pct_range: 0.01}}
        """)

    def test_a_level_that_cannot_be_read_safely_is_refused_with_words(self):
        """0.1 x 3 A is 300 mA -- above the 220 mA threshold, so there is no
        level that is both readable and safe, and the proof cannot be had."""
        source_spec = load_model("fluke5700a")
        meter_spec = parse_model_text(self.UNREADABLE, "toy-ammeter.yaml")
        point = Point(quantity=Quantity.DCI, value=0.1,
                      connection=Connection.TWO_WIRE, range_id="DC220MA")
        assert proof_level(source_spec, point, meter_spec) is None
        note = proof_note(source_spec, point, meter_spec)
        assert "0.3" in note and "0.22" in note
        assert "hazard" in note


    def test_a_run_records_the_skipped_proof_and_carries_on(self):
        """No abort, no silent skip: the row says there was no proof, in words.

        And the source is never taken to a level the meter cannot judge --
        which is what the old rule did on every 34401A ac-current group.
        """
        meter_spec = parse_model_text(self.UNREADABLE, "toy-ammeter.yaml")
        points = [PlanPoint(quantity=Quantity.DCI, value=0.1,
                            connection=Connection.TWO_WIRE, range_arg=3.0,
                            range_id="DC220MA", interval="1y",
                            label="DCI 0.1 A"),
                  PlanPoint(quantity=Quantity.DCI, value=0.15,
                            connection=Connection.TWO_WIRE, range_arg=3.0,
                            range_id="DC220MA", interval="1y",
                            label="DCI 0.15 A")]
        source = FakeSource()
        meter = FakeMeter(spec=meter_spec, source=source)
        rows = run(make_plan(points, meter_model="toy-ammeter"),
                   source, meter, meter_spec=meter_spec)
        assert len(rows) == 2
        assert FLAG_WIRING_PROOF_NOT_RESOLVABLE in rows[0].flags
        assert "no wiring proof was taken" in rows[0].note
        assert "hazard" in rows[0].note
        # Once, on the row the proof would have preceded -- not on every row.
        assert FLAG_WIRING_PROOF_NOT_RESOLVABLE not in rows[1].flags
        # Nothing but the plan's own points was ever sourced.
        assert [abs(float(p.value)) for p in source.applied] == [0.1, 0.15]


class TestGrossErrorAbort:
    """Ten tolerances is a connection, not an instrument out of spec."""

    def test_a_bare_point_gets_a_message_too(self):
        """``PointLike`` promises four attributes; ``describe()`` is not one.

        The engine takes a structural type on purpose, so a message that
        reached for a :class:`PlanPoint` method would raise on exactly the
        points the seam exists to accept.
        """
        point = Point(quantity=Quantity.DCV, value=10.0,
                      connection=Connection.TWO_WIRE, range_arg=10.0,
                      range_id="DC11V", interval="1y")
        assert not point.label
        meter = _ErrorAtMeter(at_value=10.0, gross=1e-2)
        generator = run_plan(make_plan([point]), FakeSource(), meter,
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        rows = []
        with pytest.raises(SafetyAbortError, match="DCV 10 V"):
            for row in generator:
                rows.append(row)
        assert FLAG_GROSS_ERROR_ABORT in rows[0].flags

    def test_a_twentyfold_error_stops_the_run_where_it_happened(self):
        points = [dcv_point(value=float(i + 1), label=f"p{i}")
                  for i in range(5)]
        # The 34401A's 1-year limit at 3 V is ~155 uV; 20x that is ~3.1 mV.
        meter = _ErrorAtMeter(at_value=3.0, gross=3.1e-3)
        source = FakeSource()
        generator = run_plan(make_plan(points), source, meter,
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        rows = []
        with pytest.raises(SafetyAbortError, match="times the point's"):
            for row in generator:
                rows.append(row)
        assert len(rows) == 3                       # the earlier rows survive
        assert [r.label for r in rows] == ["p0", "p1", "p2"]
        assert FLAG_GROSS_ERROR_ABORT in rows[2].flags
        assert FLAG_GROSS_ERROR_ABORT not in rows[1].flags
        # It is still recorded as the measurement it was.
        assert rows[2].error == pytest.approx(3.1e-3)
        assert rows[2].verdict is Verdict.FAIL
        assert source.log[-1] == "standby"

    def test_the_output_is_off_before_the_abort_row_is_handed_out(self):
        """A generator stops at its ``yield``, and the consumer takes its time.

        The runner writes the row, prints a line and may prompt before it
        comes back for the next one.  An abort row handed out with the
        setpoint still live -- for a gross error, which *says* the leads are
        not where the plan thinks they are -- would put all of that between
        the decision to stop and the output going off.
        """
        point = dcv_point(value=10.0, label="p0")
        meter = _ErrorAtMeter(at_value=10.0, gross=1e-2)
        source = _ZeroingSource()
        meter.source = source
        generator = run_plan(make_plan([point]), source, meter,
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        at_yield = []
        with pytest.raises(SafetyAbortError):
            for row in generator:
                at_yield.append((row, list(source.log)))
        row, log = at_yield[-1]
        assert FLAG_GROSS_ERROR_ABORT in row.flags
        assert log[-2:] == ["zero", "standby"]

    def test_a_failed_wiring_proof_de_energizes_before_its_row_too(self):
        source = _ZeroingSource()
        meter = FakeMeter(samples=[0.0], proof_reads_samples=True)
        generator = run_plan(make_plan(), source, meter,
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        at_yield = []
        with pytest.raises(SafetyAbortError):
            for row in generator:
                at_yield.append((row, list(source.log)))
        row, log = at_yield[-1]
        assert FLAG_WIRING_PROOF_FAILED in row.flags
        assert log[-2:] == ["zero", "standby"]


    def test_a_point_with_no_limit_is_judged_against_the_reference(self):
        """No usable limit: ten times **the reference component's** U.

        Not the combined U of the budget: that is Type A noise and a
        quantisation bound, tens of nanovolts, against which every ordinary
        reading is "a hundred tolerances".
        """
        # A source spec that knows nothing about current: no UNCERT?, no
        # table row, so no limit -- while the meter (a real 34401A) is still
        # a reference with a stated figure.
        point = PlanPoint(quantity=Quantity.DCI, value=0.01,
                          connection=Connection.TWO_WIRE, range_arg=0.1,
                          range_id="DC22MA", interval="1y", label="dci")
        plan = make_plan([point], dut_role="source")
        meter = _ErrorAtMeter(at_value=0.01, gross=5e-3)
        generator = run_plan(plan, FakeSource(uncertainty=None), meter,
                             source_spec=TEMPCO_CALIBRATOR,
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        rows = []
        with pytest.raises(SafetyAbortError, match="expanded uncertainty"):
            for row in generator:
                rows.append(row)
        assert rows[0].verdict is Verdict.NO_SPEC
        assert rows[0].tolerance_limit is None
        assert [c.label for c in rows[0].budget
                if c.label.startswith("reference")]      # there *is* one
        assert FLAG_GROSS_ERROR_ABORT in rows[0].flags


def _acv_source_dut_plan():
    """The shipped source-DUT ac plan: 5700A under test, 3458A as reference."""
    source_spec = load_model("fluke5700a")
    meter_spec = load_model("hp3458a")
    plan = generate_plan(source_spec, meter_spec, dut_role="source")
    points = tuple(p for p in plan.points
                   if Quantity(p.quantity) is Quantity.ACV)
    assert points                                   # the plan really has some
    return replace(plan, points=points), source_spec, meter_spec


class TestLoadingAndTheGrossErrorCheck:
    """L78/L83 x §8: the engine's own correction is not a wiring fault.

    The 5700A's millivolt ranges are a **passive** 50 ohm Thevenin source --
    "The 220 mV range is passive with an output resistance of 50 Ohm" and "In
    all cases, the output impedance of the millivolt ranges is 50 Ohm"
    (``5700a-service-manual-1996.txt`` 5304 and 5371, PDF pp. 115 and 118) --
    with no external sense below the 2.2 V range
    (``5700a-operators-manual-1996.txt`` 1772-1774, PDF p.45) and a
    calibration that determines the *unloaded* divider ratio into the internal
    cal amplifier (SM 5374-5390, PDF pp. 118-119).  The published figure is
    open-circuit value: a 3458A's "1 MOhm +/- 15 %" ac input
    (``3458a-datasheet.txt`` 968-972, PDF p.25) really does read ~50 ppm low,
    and correcting for it is not double-counting.  Contrast the wideband
    output, whose specifications "apply to the end of the cable and 50 Ohm
    termination used for calibration" (OM 2528, PDF p.60) -- there the load is
    already inside the claim.

    A fake meter that answers the open-circuit value is modelling an infinite
    input impedance it does not have; against it the *corrected* error is the
    whole correction, which is what put a `gross-error-abort` on the shipped
    plan.  The fakes model the divider, so the fake bench is the bench.
    """

    def test_the_shipped_source_dut_ac_plan_runs_to_the_end(self):
        """The reproduction: 50 ppm of loading is not ten tolerances."""
        plan, source_spec, meter_spec = _acv_source_dut_plan()
        source = FakeSource(spec=source_spec)
        meter = FakeMeter(spec=meter_spec, source=source)
        rows = run(plan, source, meter)

        assert len(rows) == len(plan.points)
        assert not [r for r in rows if FLAG_GROSS_ERROR_ABORT in r.flags]

    def test_the_correction_undoes_the_loading_it_corrects_for(self):
        """The reading is 50 ppm low; the corrected error is zero.

        ``Mean`` is what the meter read -- the loaded value, 2.2 mV across a
        50 ohm source into the 3458A's 10 mV ac range, 1 MOhm -- and ``Error``
        is taken from the corrected estimate, which is what
        ``loading-corrected`` exists to say.
        """
        plan, source_spec, meter_spec = _acv_source_dut_plan()
        point, = [p for p in plan.points
                  if p.range_id == "AC2_2MV" and p.frequency_hz == 1000.0
                  and p.value == 0.0022]
        source = FakeSource(spec=source_spec)
        meter = FakeMeter(spec=meter_spec, source=source, spread=0.0)
        row = run(replace(plan, points=(point,)), source, meter)[0]

        assert FLAG_LOADING_CORRECTED in row.flags   # 50 ppm really applied
        assert row.reference_value == pytest.approx(0.0022, rel=1e-12)
        assert row.mean == pytest.approx(0.0022 * 1e6 / (1e6 + 50.0),
                                         rel=1e-12)
        assert row.error == pytest.approx(0.0, abs=1e-15)

    def test_the_correction_has_the_same_sign_in_the_meter_dut_direction(self):
        """One divider, one direction: the loaded reading is stepped back up.

        With the meter under test the calibrator's stated value is the
        open-circuit one and the meter sees less of it, so ``reading +
        reading*Zs/Zin - actual`` is identically ``reading - actual*Zin/(Zs+Zin)
        * (1+Zs/Zin)``: the same error, whichever side is the DUT.
        """
        source_spec = load_model("fluke5700a")
        meter_spec = load_model("hp3458a")
        plan = generate_plan(source_spec, meter_spec, dut_role="meter")
        # A meter-DUT plan pins the *meter's* range and lets the calibrator
        # choose its own, so 0.1 V comes off the 5700A's 220 mV ac range --
        # 50 ohm out -- into the 3458A's 0.1 V ac range, 1 MOhm in.
        point = next(p for p in plan.points
                     if Quantity(p.quantity) is Quantity.ACV
                     and p.value == 0.1 and p.frequency_hz == 1000.0)
        source = FakeSource(spec=source_spec)
        meter = FakeMeter(spec=meter_spec, source=source, spread=0.0)
        row = run(replace(plan, points=(point,)), source, meter)[0]

        assert FLAG_LOADING_CORRECTED in row.flags
        assert row.error == pytest.approx(0.0, abs=1e-15)

    def test_the_instruments_own_claim_leaves_the_gross_check_a_wide_margin(
            self):
        """What made 50 ppm "ten tolerances" was the fake ``UNCERT?``, not physics.

        ``UNCERT_5PPM`` answers 5 ppm at every point; the 5700A's own table
        claims 100 ppm + 5 uV at 2.2 mV / 1 kHz over a year (OM 1334,
        PDF p.36) -- 2.3e3 ppm at that level.  Read with the instrument's own
        figure the whole shipped source-DUT plan stays two orders of magnitude
        inside the abort.
        """
        source_spec = load_model("fluke5700a")
        meter_spec = load_model("hp3458a")
        plan = generate_plan(source_spec, meter_spec, dut_role="source")
        source = FakeSource(spec=source_spec, uncertainty=None)
        meter = FakeMeter(spec=meter_spec, source=source)
        rows = run(plan, source, meter)

        assert len(rows) == len(plan.points)
        ratios = [abs(r.error) / r.tolerance_limit for r in rows
                  if r.error is not None and r.tolerance_limit]
        assert max(ratios) < 0.1 * GROSS_ERROR_FACTOR

    def test_the_wideband_output_is_specified_into_its_own_terminator(self):
        """L83: 50 ohm that is already inside the claim is not to be divided out.

        Every wideband range declares ``source_impedance_ohm: 50`` -- it is a
        50 ohm coax output and that is the truth about the hardware -- but its
        figures "apply to the end of the cable and 50 Ohm termination used for
        calibration" (``5700a-operators-manual-1996.txt`` 2528, PDF p.60), so
        the stated value is the value *at the load*.  Correcting it towards an
        open circuit would put a bias in the row.
        """
        point = PlanPoint(quantity=Quantity.WBV, value=0.5,
                          frequency_hz=1000.0,
                          connection=Connection.TWO_WIRE, range_arg=1.1,
                          range_id="WB1_1V", interval="1y", label="WBV 0.5 V")
        plan = make_plan([point], dut_role="source",
                         meter_model="wideband-meter")
        source = FakeSource()
        meter = FakeMeter(spec=WIDEBAND_METER, source=source, spread=0.0)
        row = run(plan, source, meter, meter_spec=WIDEBAND_METER)[0]

        assert FLAG_LOADING_CORRECTED not in row.flags
        assert FLAG_LOADING_UNCORRECTED_HF not in row.flags
        assert not [c for c in row.budget if c.label == "input loading"]
        assert row.error == pytest.approx(0.0, abs=1e-15)

    def test_a_real_gross_error_on_a_millivolt_ac_point_still_aborts(self):
        """§8 is unchanged: ten tolerances is still an abort, here too."""
        plan, source_spec, meter_spec = _acv_source_dut_plan()
        point, = [p for p in plan.points
                  if p.range_id == "AC2_2MV" and p.frequency_hz == 1000.0
                  and p.value == 0.0022]
        source = FakeSource(spec=source_spec)
        # 5 ppm of 2.2 mV is 11 pV; a whole microvolt is ~90 000 tolerances.
        meter = _ErrorAtMeter(spec=meter_spec, source=source,
                              at_value=0.0022, gross=1e-6)
        generator = run_plan(replace(plan, points=(point,)), source, meter,
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        rows = []
        with pytest.raises(SafetyAbortError, match="times the point's"):
            for row in generator:
                rows.append(row)
        assert FLAG_GROSS_ERROR_ABORT in rows[-1].flags


class TestSettling:
    """The two settling times are independent; the larger governs."""

    def test_takes_the_larger_of_the_two_needs_plus_a_margin(self):
        sleeps = []
        # The 5700A's dc volts function and its 11 V range both want 3 s.
        # ``sleeps[-1]`` is the point's; ``sleeps[0]`` is the wiring proof's.
        run(make_plan(), FakeSource(), FakeMeter(settle_s=5.0),
            sleep=sleeps.append)
        assert sleeps[-1] == pytest.approx(5.0 + DEFAULT_SETTLE_MARGIN_S)

        sleeps.clear()
        run(make_plan(), FakeSource(), FakeMeter(settle_s=0.25),
            sleep=sleeps.append)
        assert sleeps[-1] == pytest.approx(3.0 + DEFAULT_SETTLE_MARGIN_S)

    def test_a_point_may_override_both(self):
        sleeps = []
        run(make_plan([dcv_point(settle_s=12.0)]), FakeSource(),
            FakeMeter(settle_s=0.5), sleep=sleeps.append)
        assert sleeps[-1] == pytest.approx(12.0 + DEFAULT_SETTLE_MARGIN_S)
        # The proof is not the point: it takes the spec's own settling, not
        # the override the point carries.
        assert sleeps[0] == pytest.approx(3.0 + DEFAULT_SETTLE_MARGIN_S)

    def test_the_margin_is_configurable(self):
        sleeps = []
        run(make_plan(), FakeSource(), FakeMeter(settle_s=1.0),
            sleep=sleeps.append, settle_margin_s=0.0)
        assert sleeps[-1] == pytest.approx(3.0)


# --------------------------------------------------------------------------
# Bidirectionality
# --------------------------------------------------------------------------

#: 34401A, 1 year, 10 V on the 10 V range: 0.0035 % of reading + 0.0005 % of
#: range = 350 uV + 50 uV.
METER_LIMIT_10V = 35e-6 * 10.0 + 5e-6 * 10.0
#: The same meter at 24 hours: 0.0015 % + 0.0004 %.  Less than half the 1-year
#: figure, which is what makes the interval rule testable.
METER_24H_LIMIT_10V = 15e-6 * 10.0 + 4e-6 * 10.0
#: 5700A, 1 year, 10 V on the 11 V range: 8 ppm of output + 4 uV.
SOURCE_TABLE_LIMIT_10V = 8e-6 * 10.0 + 4e-6
#: The same calibrator at 24 hours: 3.5 ppm of output + 3 uV.
SOURCE_TABLE_24H_LIMIT_10V = 3.5e-6 * 10.0 + 3e-6
#: What the fake ``UNCERT?`` says about the same output.
SOURCE_UNCERT_10V = 5e-6 * 10.0


class TestDirection:
    """``dut_role`` swaps the limit and the reference -- and nothing else."""


    def test_the_error_is_meter_minus_source_in_both_directions(self):
        for dut_role in ("meter", "source"):
            rows = run(make_plan(dut_role=dut_role),
                       FakeSource(), FakeMeter(error=+2.0e-4))
            assert rows[0].error == pytest.approx(2.0e-4), dut_role
            rows = run(make_plan(dut_role=dut_role),
                       FakeSource(), FakeMeter(error=-2.0e-4))
            assert rows[0].error == pytest.approx(-2.0e-4), dut_role


    def test_the_source_table_is_always_stored_as_the_cross_check(self):
        for dut_role in ("meter", "source"):
            row = run(make_plan(dut_role=dut_role))[0]
            assert row.spec_limit == pytest.approx(SOURCE_TABLE_LIMIT_10V)
            assert row.source_uncert_limit == pytest.approx(SOURCE_UNCERT_10V)
            # 84 uV against 50 uV is well inside the factor of two.
            assert row.limit_disagree is False

    def test_a_zero_setpoint_source_dut_point_is_judged_not_failed(self):
        """The **defensive** path: a source that answers no figure at zero.

        The bench 5700A answers the range's absolute floor in volts at a zero
        setpoint (``UNCERT? +7.5000E-07,V,1``, capture 2026-08-16), which is
        the case below this one.  A *relative* answer at a zero setpoint --
        ppm of zero -- is zero, and a tolerance of zero is not a tight claim
        but the absence of one: no measurement can lie inside +/-0, so
        preferring it over the spec table failed every zero setpoint
        unconditionally.  A generated source-DUT plan puts a zero on every dc
        range and the short: eleven of the 156 shipped points, in the
        direction the how-to recommends first.

        The table carries the absolute floor these points are actually
        specified by: 100 uV on the dc 220 V range at 1 y, 0.8 uV on the
        220 mV range.
        """
        top = PlanPoint(quantity=Quantity.DCV, value=0.0,
                        connection=Connection.TWO_WIRE, range_id="DC220V",
                        interval="1y", label="DCV 0 V on 220 V")
        bottom = PlanPoint(quantity=Quantity.DCV, value=0.0,
                           connection=Connection.TWO_WIRE, range_id="DC220MV",
                           interval="1y", label="DCV 0 V on 220 mV")
        plan = make_plan([top, bottom], dut_role="source",
                         meter_model="hp3458a")
        rows = run(plan, FakeSource(), FakeMeter(load_model("hp3458a"),
                                                 spread=1e-9))

        for row in rows:
            # The instrument's own figure really is zero here -- this is the
            # value the limit used to be taken from.
            assert row.source_uncert_limit == 0.0
            assert FLAG_LIMIT_FROM_SPEC_TABLE in row.flags
            assert row.tolerance_limit is not None
            assert row.tolerance_limit > 0.0
            assert row.verdict is not Verdict.FAIL

        # 220 V range: a 100 uV floor against a 3458A reading zero on its
        # 100 mV range is a comfortable ratio, so the point can PASS.
        assert rows[0].tolerance_limit == pytest.approx(1e-4)
        assert rows[0].verdict is Verdict.PASS

        # 220 mV range: the floor is 0.8 uV and the same reference is only
        # 0.3 uV, so the honest answer is INDETERMINATE on the TUR floor --
        # judged, which is the point, rather than failed by arithmetic.
        assert rows[1].tolerance_limit == pytest.approx(8e-7)
        assert rows[1].verdict is Verdict.INDETERMINATE
        assert rows[1].indeterminate_by_tur

    def test_an_absolute_uncert_at_a_zero_setpoint_is_the_claim(self):
        """M82: what the instrument actually answers at 0 V.

        ``UNCERT? +7.5000E-07,V,1`` at ``OUT 0 V`` on the 220 mV range -- the
        range's 24-hour absolute floor, in volts, not ppm of zero.  This is
        the live path on the bench and it had no test at all.
        """
        floor = Uncertainty(value=7.5e-7, unit="V", confidence=0.99,
                            interval_days=1)
        point = PlanPoint(quantity=Quantity.DCV, value=0.0,
                          connection=Connection.TWO_WIRE, range_id="DC220MV",
                          interval="1y", label="DCV 0 V on 220 mV")
        plan = make_plan([point], dut_role="source", meter_model="hp3458a")
        row = run(plan, FakeSource(zero_uncertainty=floor),
                  FakeMeter(load_model("hp3458a"), spread=1e-9))[0]
        assert row.source_uncert_limit == pytest.approx(7.5e-7)
        assert row.tolerance_limit == pytest.approx(7.5e-7)
        assert FLAG_LIMIT_FROM_SPEC_TABLE not in row.flags

    def test_a_relative_zero_is_no_figure_not_a_zero_reference(self):
        """M51/M53: ppm of zero is zero, and a zero reference term is none.

        Nothing else in the budget covers the calibrator, so accepting it
        would hand out a reference contribution of exactly 0 -- U understated
        by an order of magnitude and a TUR of 50:1 where the real ratio is
        below 1, with no flag anywhere.
        """
        point = PlanPoint(quantity=Quantity.DCV, value=0.0,
                          connection=Connection.TWO_WIRE, range_id="DC220MV",
                          interval="1y", label="DCV 0 V on 220 mV")
        plan = make_plan([point], dut_role="meter", meter_model="hp3458a")
        row = run(plan, FakeSource(), FakeMeter(load_model("hp3458a"),
                                                spread=1e-9))[0]
        assert row.source_uncert_limit == 0.0            # ppm of zero
        assert FLAG_REFERENCE_FROM_SPEC_TABLE in row.flags
        reference, = [c for c in row.budget if c.label.startswith("reference")]
        assert reference.label == "reference (fluke5700a spec)"
        # The 220 mV range's own absolute floor, not zero.
        assert reference.value > 0.0

    def test_the_zero_point_fallback_follows_the_instruments_own_interval(self):
        """M64: the claim under test is UNCERT? at *its* CAL_INTV, not the plan's.

        On the bench unit ``CAL_INTV`` is 1 day: every non-zero point of a
        ``--dut source`` run is judged against the 24-hour column whatever the
        plan's header says, so judging its zero points against the 1-year
        floor put two different claims in one certificate.
        """
        relative_zero = Uncertainty(value=5.0, unit="PPM", confidence=0.99,
                                    interval_days=1)
        point = PlanPoint(quantity=Quantity.DCV, value=0.0,
                          connection=Connection.TWO_WIRE, range_id="DC220MV",
                          interval="1y", label="DCV 0 V on 220 mV")
        plan = make_plan([point], dut_role="source", meter_model="hp3458a",
                         interval="1y")
        row = run(plan, FakeSource(zero_uncertainty=relative_zero),
                  FakeMeter(load_model("hp3458a"), spread=1e-9))[0]
        assert FLAG_LIMIT_FROM_SPEC_TABLE in row.flags
        # 24 h floor, not the 1 y one the plan's interval would have chosen.
        assert row.tolerance_limit == pytest.approx(7.5e-7)
        assert row.tolerance_limit != pytest.approx(8e-7)

    def test_without_an_interval_the_plans_column_still_answers(self):
        relative_zero = Uncertainty(value=5.0, unit="PPM", confidence=0.99)
        point = PlanPoint(quantity=Quantity.DCV, value=0.0,
                          connection=Connection.TWO_WIRE, range_id="DC220MV",
                          interval="1y", label="DCV 0 V on 220 mV")
        plan = make_plan([point], dut_role="source", meter_model="hp3458a",
                         interval="1y")
        row = run(plan, FakeSource(zero_uncertainty=relative_zero),
                  FakeMeter(load_model("hp3458a"), spread=1e-9))[0]
        assert row.tolerance_limit == pytest.approx(8e-7)

    def test_a_positive_uncert_is_not_widened_by_a_looser_table(self):
        """The instrument is the authority where it states a figure.

        ``limits_authority: instrument`` on the 5700A: taking whichever of
        UNCERT? and the table is looser would silently widen the claim under
        test -- most plausibly when the unit's CAL_INTV is not the plan's
        interval, which is what ``limit_disagree`` is for.
        """
        row = run(make_plan(dut_role="source"))[0]
        assert row.tolerance_limit == pytest.approx(SOURCE_UNCERT_10V)
        assert row.spec_limit == pytest.approx(SOURCE_TABLE_LIMIT_10V)
        assert SOURCE_TABLE_LIMIT_10V > SOURCE_UNCERT_10V
        assert FLAG_LIMIT_FROM_SPEC_TABLE not in row.flags

    def test_a_source_with_no_uncert_falls_back_to_its_table(self):
        source = FakeSource(uncertainty=None)
        row = run(make_plan(dut_role="source"), source)[0]
        assert FLAG_LIMIT_FROM_SPEC_TABLE in row.flags
        assert row.tolerance_limit == pytest.approx(SOURCE_TABLE_LIMIT_10V)

    def test_a_meter_dut_falls_back_to_the_source_table_for_the_reference(self):
        source = FakeSource(uncertainty=None)
        row = run(make_plan(dut_role="meter"), source)[0]
        assert FLAG_REFERENCE_FROM_SPEC_TABLE in row.flags
        reference, = [c for c in row.budget if c.label.startswith("reference")]
        assert reference.label == "reference (fluke5700a spec)"
        # Not sqrt(3): the 5700A *states* the confidence its tables are
        # written to, and fluke5700a.yaml now carries it as
        # `spec_confidence: 0.99` ("Fluke guarantees performance verification
        # using specifications stated to 99% confidence level").  The ruling
        # is that a stated confidence is used where the manufacturer states
        # one and a rectangular bound only where none is stated, so the
        # divisor here is the 99 % normal quantile, 2.576.
        assert reference.distribution == "normal"
        assert reference.value == pytest.approx(
            SOURCE_TABLE_LIMIT_10V / normal_coverage_factor(0.99))

    def test_an_uncert_with_no_confidence_is_not_assumed_to_be_95_percent(self):
        source = FakeSource(uncertainty=Uncertainty(value=5.0, unit="PPM"))
        row = run(make_plan(dut_role="meter"), source)[0]
        assert FLAG_REFERENCE_CONFIDENCE_UNKNOWN in row.flags
        reference, = [c for c in row.budget if c.label.startswith("reference")]
        assert reference.distribution == "rectangular"
        assert reference.value == pytest.approx(SOURCE_UNCERT_10V
                                                / math.sqrt(3))


class TestLoadCurrentDerating:
    """H06: the plan's ``K(IL - I)/(IL x I)`` reaches the calibrator's figure.

    The 5700A's resistance uncertainty is stated **only** inside its Full Spec
    Load Range ([OM 1996] p.49-50).  Nothing in the calibrator can know what
    current the paired meter sources, so ``UNCERT?`` cannot fold the derating
    in and plan generation carries it instead, as
    :attr:`~caldmm.cal.plan.PlanPoint.load_adder` (absolute, in the quantity's
    own unit) plus a note that promises it will be added.  A plan that promises
    an adder and an engine that never applies it is a budget that understates
    the reference -- or a tolerance that understates the claim -- at exactly
    the points the manual singles out.

    The points here are the **generated** ones, not hand-built: the number
    under test is the one a real run would carry, and the test breaks if the
    plan side stops producing it.
    """

    #: 19 ohm read on a 3458A 100 ohm range at 1 mA against IL = 8 mA:
    #: ``16 x (8 - 1)/(8 x 1)`` = 14 ppm of 19 ohm = 266 uohm.
    ADDER = 14e-6 * 19.0

    @staticmethod
    def point(dut_role):
        plan = generate_plan(load_model("fluke5700a"), load_model("hp3458a"),
                             dut_role=dut_role, quantities=[Quantity.RES])
        return next(p for p in plan.points if p.value == 19.0)

    def row(self, dut_role, point=None, source=None):
        point = self.point(dut_role) if point is None else point
        plan = make_plan([point], dut_role=dut_role, meter_model="hp3458a")
        return run(plan, FakeSource() if source is None else source,
                   FakeMeter(load_model("hp3458a")))[0]

    def test_the_generated_point_carries_the_adder_in_both_roles(self):
        for dut_role in ("meter", "source"):
            point = self.point(dut_role)
            assert point.load_adder == pytest.approx(self.ADDER), dut_role
            assert "full-spec load" in point.note, dut_role

    def test_the_reference_budget_carries_it_at_uncerts_confidence(self):
        """Meter under test: the calibrator is the reference, and the derating
        is a further Type B term on *its* side, at the level ``UNCERT?`` is
        quoted to."""
        row = self.row("meter")
        assert FLAG_LOAD_DERATED in row.flags
        derating, = [c for c in row.budget
                     if c.label.startswith("load derating")]
        assert derating.label == "load derating (fluke5700a)"
        assert derating.type == "B"
        assert derating.distribution == "normal"
        assert derating.value == pytest.approx(
            self.ADDER / normal_coverage_factor(0.95))
        assert "UNCERT?" in derating.source
        assert "95 %" in derating.source

    def test_a_table_reference_derates_at_the_tables_confidence(self):
        """No ``UNCERT?``: the reference is the spec table, whose stated 99 %
        is the level the adder is a bound at too."""
        row = self.row("meter", source=FakeSource(uncertainty=None))
        assert FLAG_REFERENCE_FROM_SPEC_TABLE in row.flags
        assert FLAG_LOAD_DERATED in row.flags
        derating, = [c for c in row.budget
                     if c.label.startswith("load derating")]
        assert derating.value == pytest.approx(
            self.ADDER / normal_coverage_factor(0.99))
        assert "spec table" in derating.source

    def test_an_unstated_confidence_keeps_the_adder_rectangular(self):
        """The reference's own treatment, followed: a bound with no level
        stated is rectangular, and the adder is not given one either."""
        source = FakeSource(uncertainty=Uncertainty(value=5.0, unit="PPM"))
        row = self.row("meter", source=source)
        assert FLAG_REFERENCE_CONFIDENCE_UNKNOWN in row.flags
        derating, = [c for c in row.budget
                     if c.label.startswith("load derating")]
        assert derating.distribution == "rectangular"
        assert derating.value == pytest.approx(self.ADDER / math.sqrt(3))

    def test_the_meter_dut_tolerance_is_not_touched(self):
        """The adder is the *calibrator's*.  Widening the meter's own claim
        with it would let a load condition excuse the instrument under test."""
        point = self.point("meter")
        derated = self.row("meter", point)
        plain = self.row("meter", replace(point, load_adder=None))
        assert derated.tolerance_limit == pytest.approx(plain.tolerance_limit)

    def test_the_source_dut_limit_is_widened_by_the_adder(self):
        """Calibrator under test: the manual's accuracy statement at this load
        is the stated figure **plus** the derating, so that is the claim."""
        point = self.point("source")
        derated = self.row("source", point)
        plain = self.row("source", replace(point, load_adder=None))
        assert FLAG_LOAD_DERATED in derated.flags
        assert plain.tolerance_limit == pytest.approx(5e-6 * 19.0)
        assert derated.tolerance_limit == pytest.approx(
            plain.tolerance_limit + self.ADDER)
        # ... and the verdict is taken against the widened limit, not beside
        # it: the margin moves by the whole adder.
        assert derated.margin == pytest.approx(plain.margin + self.ADDER)

    def test_it_is_applied_once_not_twice(self):
        """Whichever role the calibrator is in, the adder enters exactly one
        place: the budget when it is the reference, the tolerance when it is
        the DUT."""
        source_dut = self.row("source")
        assert not [c for c in source_dut.budget
                    if c.label.startswith("load derating")]
        # ... and the calibrator's own table figure is still stored as the
        # cross-check exactly as the table writes it, in both roles: the
        # adder is a test condition, not a re-transcription of the table.
        for dut_role in ("meter", "source"):
            point = self.point(dut_role)
            derated = self.row(dut_role, point)
            plain = self.row(dut_role, replace(point, load_adder=None))
            assert derated.spec_limit == pytest.approx(plain.spec_limit), \
                dut_role


    def test_a_point_with_no_adder_is_unchanged(self):
        for dut_role in ("meter", "source"):
            point = replace(self.point(dut_role), load_adder=None)
            row = self.row(dut_role, point)
            assert FLAG_LOAD_DERATED not in row.flags, dut_role
            assert not [c for c in row.budget
                        if c.label.startswith("load derating")], dut_role


# --------------------------------------------------------------------------
# Ambient temperature
# --------------------------------------------------------------------------

class TestAmbient:
    """The probe is a parameter, and it decides whether a PASS is possible."""


    def test_the_probe_is_recorded_with_its_provenance(self):
        row = run(make_plan(), environment=FakeProbe(22.5, source="probe"))[0]
        assert row.env_source == "probe"
        assert row.humidity_pct == 45.0
        assert row.env_timestamp is not None


    def test_a_fresh_reading_is_reused_within_a_group(self):
        probe = FakeProbe(23.0)
        points = [dcv_point(value=float(i + 1)) for i in range(5)]
        run(make_plan(points), environment=probe,
            environment_interval_s=600.0)
        assert probe.calls == 1                      # one group, one read


    def test_a_failing_probe_is_flagged_not_fatal(self):
        probe = FakeProbe(error=RuntimeError("the database is down"))
        rows = run(make_plan(), environment=probe)
        assert len(rows) == 1
        assert FLAG_AMBIENT_READ_FAILED in rows[0].flags
        assert rows[0].ambient_c is None
        assert rows[0].verdict is Verdict.INDETERMINATE


# --------------------------------------------------------------------------
# The budget
# --------------------------------------------------------------------------

class TestBudget:
    """Every term comes from a builder in ``uncertainty``."""


    def test_a_single_sample_has_no_type_a_term_and_says_so(self):
        row = run(make_plan(samples=1))[0]
        assert FLAG_NO_TYPE_A in row.flags
        assert not [c for c in row.budget if c.type == "A"]
        assert row.n == 1

    def test_loading_is_corrected_where_both_impedances_are_known(self):
        # The 5700A's 220 mV range is 50 ohm out; the 34401A's 100 V range is
        # a fixed 10 M.  Use the 220 mV source range against that meter range
        # so the divider is real, and check the correction is applied and
        # carried rather than merely bounded.
        point = PlanPoint(quantity=Quantity.DCV, value=0.1,
                          connection=Connection.TWO_WIRE, range_arg=100.0,
                          range_id="DC220MV", interval="1y")
        rows = run(make_plan([point]), FakeSource(), FakeMeter(spread=0.0))
        row = rows[0]
        assert FLAG_LOADING_CORRECTED in row.flags
        loading, = [c for c in row.budget if c.label == "input loading"]
        # The meter is across the divider, so it reads the loaded value and
        # the correction is taken on *that*: reading * Zs/Zin, u = 10 % of it,
        # rectangular.
        loaded = 0.1 * 1e7 / (1e7 + 50.0)
        assert loading.value == pytest.approx(loaded * 5e-6 * 0.1
                                              / math.sqrt(3))
        # ``Mean`` is what was read; ``Error`` is taken after the correction,
        # which is exactly the loading and therefore leaves nothing behind.
        assert row.mean == pytest.approx(loaded, rel=1e-12)
        assert row.error == pytest.approx(0.0, abs=1e-15)


    def test_no_loading_correction_above_a_hundred_kilohertz(self):
        """L78: correcting the small term while ignoring the large one.

        Into the 5700A's 50 Ohm mV ac output the ignored capacitive loading is
        ~1000 ppm at 1 MHz where the resistive correction is ~50 ppm -- 20
        times the thing being corrected -- and the model files carry no input
        capacitance to model it with.
        """
        point = PlanPoint(quantity=Quantity.ACV, value=0.1,
                          frequency_hz=1.0e6,
                          connection=Connection.TWO_WIRE, range_arg=0.1,
                          range_id="AC220MV", interval="1y", label="ACV 1 MHz")
        # The calibrator's own claim, from its table: ``UNCERT_5PPM`` answers
        # 5 ppm at every point, and against 5 ppm the residual below is ten
        # tolerances -- which says everything about that fake and nothing
        # about the 5700A, whose 220 mV ac figure at 1 MHz is 3300 ppm + 60 uV
        # (``5700a-operators-manual-1996.txt`` 1355, PDF p.36).
        row = run(make_plan([point], dut_role="source"),
                  FakeSource(uncertainty=None), FakeMeter(spread=0.0))[0]
        assert FLAG_LOADING_UNCORRECTED_HF in row.flags
        assert FLAG_LOADING_CORRECTED not in row.flags
        assert not [c for c in row.budget if c.label == "input loading"]
        # ... and the reading is left as it was read, so the row keeps the
        # residual the flag exists to declare: the meter really is across a
        # 50 ohm source and really does read low.
        assert row.error == pytest.approx(-0.1 * 50.0 / (1e6 + 50.0),
                                          rel=1e-12)

    def test_no_loading_term_where_an_impedance_is_unknown(self):
        row = run(make_plan())[0]                    # 5700A 11 V range: no Zs
        assert FLAG_LOADING_CORRECTED not in row.flags
        assert not [c for c in row.budget if c.label == "input loading"]

    def test_thermal_emf_is_carried_when_the_operator_states_one(self):
        row = run(make_plan(), thermal_emf=2e-6)[0]
        emf, = [c for c in row.budget if c.label == "thermal emf"]
        assert emf.value == pytest.approx(2e-6 / math.sqrt(3))
        # The default bound is TestThermalEmf's; this is the operator's.
        assert FLAG_THERMAL_EMF_DEFAULT not in row.flags

    def test_the_reference_tempco_is_flagged_when_delta_t_is_unknown(self):
        # A 3458A as the reference: its figures are relative to its ACAL
        # temperature, which no model file can state.
        plan = make_plan(dut_role="source", meter_model="hp3458a")
        row = run(plan, FakeSource(), FakeMeter(load_model("hp3458a")))[0]
        assert FLAG_TEMPCO_NOT_EVALUATED in row.flags
        assert not [c for c in row.budget if "tempco" in c.label]

    def test_the_reference_tempco_is_evaluated_when_tcal_is_supplied(self):
        """The term appears; *how far outside* it is measured from is below.

        The exact dead band is a property of the model file (whether it
        declares a window at all, and how wide), so it is pinned on the toy
        models in :class:`TestTempcoDeadBand` rather than here, where a
        transcription change would silently re-write the expected number.
        """
        plan = make_plan(dut_role="source", meter_model="hp3458a")
        row = run(plan, FakeSource(), FakeMeter(load_model("hp3458a")),
                  reference_tempco_ref_c=20.0, environment=FakeProbe(40.0))[0]
        assert FLAG_TEMPCO_NOT_EVALUATED not in row.flags
        tempco, = [c for c in row.budget if "tempco" in c.label]
        assert tempco.value > 0.0
        assert tempco.distribution == "rectangular"

    def test_a_calibrator_reference_carries_no_tempco_term(self):
        # UNCERT? already includes the temperature coefficient; adding one
        # would double-count it.
        row = run(make_plan(dut_role="meter"))[0]
        assert not [c for c in row.budget if "tempco" in c.label]
        assert FLAG_TEMPCO_NOT_EVALUATED not in row.flags


class TestStatedConfidence:
    """M54: a table stated at 99 % is not a rectangular bound.

    Dividing a 99 % figure by sqrt(3) overstates the standard uncertainty by
    49 % -- conservative, so never a false PASS, but it contradicts the
    module's own doctrine, depresses the TUR and turns PASS into
    INDETERMINATE at the many points already near the 4:1 floor.  Where the
    manufacturer states nothing (3458A, 34420A) rectangular is still right.
    """

    def test_a_stated_confidence_is_used_instead_of_rectangular(self):
        row = run(make_plan(dut_role="meter"), FakeSource(uncertainty=None),
                  source_spec=STATED_CONFIDENCE_SOURCE)[0]
        assert FLAG_REFERENCE_FROM_SPEC_TABLE in row.flags
        reference, = [c for c in row.budget if c.label.startswith("reference")]
        limit = 8e-6 * 10.0 + 4e-6
        assert reference.distribution == "normal"
        assert reference.value == pytest.approx(limit / 2.5758293035489004)
        assert "99 %" in reference.source

    def test_no_stated_confidence_stays_rectangular(self):
        row = run(make_plan(dut_role="meter"), FakeSource(uncertainty=None),
                  source_spec=TEMPCO_CALIBRATOR)[0]
        reference, = [c for c in row.budget
                      if c.label == "reference (tc-calibrator spec)"]
        assert reference.distribution == "rectangular"
        limit = 8e-6 * 10.0 + 4e-6
        assert reference.value == pytest.approx(limit / math.sqrt(3))

    def test_the_meter_as_reference_uses_its_own_stated_confidence(self):
        plan = make_plan(dut_role="source", meter_model="traceable-meter")
        row = run(plan, FakeSource(), FakeMeter(TRACEABLE_METER))[0]
        reference, = [c for c in row.budget
                      if c.label == "reference (traceable-meter spec)"]
        limit = 35e-6 * 10.0 + 5e-6 * 10.0
        assert reference.distribution == "normal"
        assert reference.value == pytest.approx(limit / 1.959963984540054)


class TestTraceability:
    """M55/M75: a meter's figures are relative to its own standards.

    "Absolute accuracy of the 3458A is determined by adding these relative
    accuracies to the traceability of your calibration standard"
    (``3458a-datasheet.txt:282-287``).  In a source-DUT run the comparison is
    absolute, so that term is a required GUM input -- and leaving it out
    understates u_ref, which is the unsafe direction.
    """

    def _budget(self, **kwargs):
        plan = make_plan(dut_role="source", meter_model="traceable-meter")
        return run(plan, FakeSource(), FakeMeter(TRACEABLE_METER), **kwargs)[0]

    def test_it_is_a_separate_component_when_the_meter_is_the_reference(self):
        row = self._budget()
        trace, = [c for c in row.budget if "traceability" in c.label]
        assert trace.label == "reference traceability (traceable-meter)"
        assert trace.value == pytest.approx(2e-6 * 10.0 / 2.5758293035489004)
        assert "factory to NIST" in trace.source
        # ... and it is *not* folded into the tolerance.
        assert row.tolerance_limit == pytest.approx(5e-6 * 10.0)

    def test_it_is_absent_when_the_meter_is_the_one_under_test(self):
        plan = make_plan(dut_role="meter", meter_model="traceable-meter")
        row = run(plan, FakeSource(), FakeMeter(TRACEABLE_METER))[0]
        assert not [c for c in row.budget if "traceability" in c.label]

    def test_the_operator_can_override_it_from_a_certificate(self):
        """The data sheet's figure is the *factory*'s; a recalibrated unit's
        "will likely be different", and the certificate is the authority."""
        row = self._budget(reference_traceability=Traceability(
            ppm_reading=5.0, k=2.0, note="cert 2026-03-11"))
        trace, = [c for c in row.budget if "traceability" in c.label]
        assert trace.value == pytest.approx(5e-6 * 10.0 / 2.0)
        assert "cert 2026-03-11" in trace.source


class TestThermalEmf:
    """M63: the term that dominates a zero or a short is not opt-in.

    A budget that omits its largest component is not GUM-complete (JCGM 100
    3.4.1/4.3.1), and at a zero point on the 220 mV range a 1 uV
    uncompensated thermal exceeds the whole tolerance.  The bound is declared
    by default and flagged as declared, so a reader can see it was assumed
    rather than measured.
    """

    def test_the_bound_defaults_to_one_microvolt_and_says_so(self):
        row = run(make_plan())[0]
        emf, = [c for c in row.budget if c.label == "thermal emf"]
        assert emf.value == pytest.approx(1e-6 / math.sqrt(3))
        assert FLAG_THERMAL_EMF_DEFAULT in row.flags

    def test_an_operator_bound_is_used_and_not_flagged_as_a_default(self):
        row = run(make_plan(), thermal_emf=2e-6)[0]
        emf, = [c for c in row.budget if c.label == "thermal emf"]
        assert emf.value == pytest.approx(2e-6 / math.sqrt(3))
        assert FLAG_THERMAL_EMF_DEFAULT not in row.flags

    def test_an_explicit_zero_disables_it(self):
        row = run(make_plan(), thermal_emf=0.0)[0]
        assert not [c for c in row.budget if c.label == "thermal emf"]
        assert FLAG_THERMAL_EMF_DEFAULT not in row.flags

    def test_a_low_ohms_point_says_the_bound_could_not_be_converted(self):
        """L84: emf/I_test needs a test current no model file states."""
        point = PlanPoint(quantity=Quantity.RES, value=10.0,
                          connection=Connection.FOUR_WIRE, range_id="OHM10",
                          range_arg=10.0, interval="1y", label="RES 10 OHM")
        plan = make_plan([point], meter_model="hp3458a")
        row = run(plan, FakeSource(), FakeMeter(load_model("hp3458a")))[0]
        assert not [c for c in row.budget if c.label == "thermal emf"]
        assert FLAG_THERMAL_EMF_NOT_CONVERTED in row.flags

    def test_a_high_resistance_point_is_not_burdened_with_the_note(self):
        point = PlanPoint(quantity=Quantity.RES, value=1e6,
                          connection=Connection.FOUR_WIRE, range_id="OHM1M",
                          range_arg=1e6, interval="1y", label="RES 1 MOHM")
        plan = make_plan([point], meter_model="hp3458a")
        row = run(plan, FakeSource(), FakeMeter(load_model("hp3458a")))[0]
        assert FLAG_THERMAL_EMF_NOT_CONVERTED not in row.flags


class TestTempcoDoubleCountGuard:
    """The ``UNCERT?``-versus-spec-table branch, against a spec that has one.

    ``fluke5700a.yaml`` declares no ``tempco`` anywhere, so the assertion above
    -- and every other calibrator-reference test -- passes for the wrong
    reason: there is nothing to double-count, and deleting the guard in
    ``_build_budget`` left the whole suite green.  These two run the same code
    against :data:`TEMPCO_CALIBRATOR`, a source spec that *does* declare one, so
    the "no term" case is a real refusal and the "term" case proves the same
    number was there to be added.
    """

    def _row(self, uncertainty):
        return run(make_plan(dut_role="meter"),
                   FakeSource(uncertainty=uncertainty),
                   source_spec=TEMPCO_CALIBRATOR,
                   reference_tempco_ref_c=20.0,
                   environment=FakeProbe(23.0))[0]

    @staticmethod
    def _tempco_terms(row):
        # startswith, not "in": the model id appears in the *reference* label
        # too, and matching that would make both tests assert the opposite of
        # what they say.
        return [c for c in row.budget if c.label.startswith("reference tempco")]

    def test_an_uncert_reference_still_carries_no_tempco_term(self):
        """``UNCERT?`` answered, so its figure already includes the tempco."""
        row = self._row(UNCERT_5PPM)
        assert FLAG_REFERENCE_FROM_SPEC_TABLE not in row.flags
        assert self._tempco_terms(row) == []
        assert FLAG_TEMPCO_NOT_EVALUATED not in row.flags

    def test_a_spec_table_reference_does_carry_one(self):
        """No ``UNCERT?``: the reference came from the file, tempco and all.

        This is what makes the test above mean something -- the same spec, the
        same 3 C departure, and here the term appears.
        """
        row = self._row(None)
        assert FLAG_REFERENCE_FROM_SPEC_TABLE in row.flags
        tempco, = self._tempco_terms(row)
        # 0.2 ppm of reading + 0.05 ppm of range per C on the 11 V range,
        # over 23 - 20 = 3 C, rectangular.
        half_width = (0.2e-6 * 10.0 + 0.05e-6 * 11.0) * 3.0
        assert tempco.value == pytest.approx(half_width / math.sqrt(3))


class TestTempcoDeadBand:
    """M57/L107/L116/L77: the coefficient is an adder *outside* the window.

    Every data sheet says so -- "Operating temperature is 10 C beyond the
    standard range of Tcal +/- 5 C ... x 10 C" (3458A datasheet Example 4,
    ``3458a-datasheet.txt:355-366``), "outside a 23 C +/- 5 C temperature
    range" (``34401a-user.txt:9619-9624``), tempco column "28 C to 55 C"
    (``34420a-datasheet.txt:129``).  Applying it over the whole departure
    inflates U for a reason the manufacturer does not claim, and points near
    the TUR floor flip to INDETERMINATE for an effect the datasheet says is
    zero.
    """

    def _row(self, ambient_c):
        plan = make_plan(dut_role="source", meter_model="windowed-meter")
        return run(plan, FakeSource(), FakeMeter(WINDOWED_METER),
                   environment=FakeProbe(ambient_c))[0]

    @staticmethod
    def _terms(row):
        return [c for c in row.budget if c.label.startswith("reference tempco")]

    def test_inside_the_window_there_is_no_tempco_term(self):
        row = self._row(25.0)
        assert self._terms(row) == []
        assert FLAG_TEMPCO_NOT_EVALUATED not in row.flags

    def test_above_the_window_the_adder_runs_from_the_window_edge(self):
        row = self._row(31.0)                     # 3 C above the 28 C edge
        tempco, = self._terms(row)
        half_width = (6e-6 * 10.0 + 1e-6 * 10.0) * 3.0
        assert tempco.value == pytest.approx(half_width / math.sqrt(3))
        assert "beyond" in tempco.source

    def test_below_the_window_it_runs_from_the_low_edge(self):
        row = self._row(15.0)                     # 3 C below the 18 C edge
        tempco, = self._terms(row)
        half_width = (6e-6 * 10.0 + 1e-6 * 10.0) * 3.0
        assert tempco.value == pytest.approx(half_width / math.sqrt(3))

    def test_a_model_with_no_window_still_counts_the_whole_departure(self):
        """No window declared is not "the departure is exempt"."""
        row = run(make_plan(dut_role="meter"), FakeSource(uncertainty=None),
                  source_spec=TEMPCO_CALIBRATOR, reference_tempco_ref_c=20.0,
                  environment=FakeProbe(23.0))[0]
        tempco, = [c for c in row.budget
                   if c.label.startswith("reference tempco")]
        half_width = (0.2e-6 * 10.0 + 0.05e-6 * 11.0) * 3.0
        assert tempco.value == pytest.approx(half_width / math.sqrt(3))


class TestSourceTemperatureWindow:
    """M50/M56/M62: the 5700A's window is relative to its own CAL_TEMP.

    Every 5700A table is headed "Absolute Uncertainty +/- 5 C from calibration
    temperature" and the tempco note says the adder "does not apply unless
    operating more than +/-5 C from calibration temperature" (OM 1996 lines
    1219 and 1301-1302).  Both numbers -- CAL_TEMP? and the ambient -- were
    already read and recorded; nothing differenced them.
    """

    def test_the_source_under_test_is_gated_on_its_own_cal_temp(self):
        plan = make_plan(dut_role="source",
                         meter_model="windowed-meter")
        source = _CalTempSource(21.1)
        row = run(plan, source, FakeMeter(WINDOWED_METER),
                  source_spec=RELATIVE_WINDOW_SOURCE,
                  environment=FakeProbe(27.0))[0]
        assert row.temperature_status is TemperatureStatus.OUT_OF_WINDOW
        assert row.verdict is Verdict.INDETERMINATE
        assert FLAG_CONDITIONS_NOT_MET in row.flags

    def test_inside_that_window_the_point_is_judged(self):
        plan = make_plan(dut_role="source", meter_model="windowed-meter")
        source = _CalTempSource(21.1)
        row = run(plan, source, FakeMeter(WINDOWED_METER),
                  source_spec=RELATIVE_WINDOW_SOURCE,
                  environment=FakeProbe(23.0))[0]
        assert row.temperature_status is TemperatureStatus.IN_WINDOW
        assert FLAG_CONDITIONS_NOT_MET not in row.flags

    def test_the_source_as_reference_is_gated_too(self):
        """M56: the reference's window is a condition of the claim as well."""
        source = _CalTempSource(21.1)
        row = run(make_plan(dut_role="meter"), source,
                  source_spec=RELATIVE_WINDOW_SOURCE,
                  environment=FakeProbe(27.0))[0]
        assert row.verdict is Verdict.INDETERMINATE
        assert FLAG_REFERENCE_OUT_OF_WINDOW in row.flags
        assert "reference" in row.reason and "27" in row.reason

    def test_a_source_that_states_no_cal_temperature_is_not_invented(self):
        row = run(make_plan(dut_role="meter"), FakeSource(),
                  source_spec=RELATIVE_WINDOW_SOURCE,
                  environment=FakeProbe(27.0))[0]
        assert FLAG_REFERENCE_OUT_OF_WINDOW not in row.flags

    def test_the_cal_temperature_is_read_once_per_run(self):
        points = [dcv_point(value=float(i + 1)) for i in range(3)]
        source = _CalTempSource(21.1)
        run(make_plan(points, dut_role="meter"), source,
            source_spec=RELATIVE_WINDOW_SOURCE, environment=FakeProbe(23.0))
        assert source.cal_temp_calls == 1


class TestOverloads:
    """Sentinels are excluded, counted and carried into the row."""

    def test_overload_counts_propagate(self):
        rows = run(make_plan(), FakeSource(), FakeMeter(overloads=2))
        assert rows[0].overloads == 2
        assert rows[0].n == 4                        # the good ones survived
        assert FLAG_OVERLOAD_SAMPLES in rows[0].flags
        assert summarize_run(rows).overloads == 2

    def test_a_point_of_nothing_but_sentinels_is_a_failed_point(self):
        meter = FakeMeter(samples=[9.9e37, 9.9e37])
        rows = run(make_plan(), FakeSource(), meter)
        assert FLAG_POINT_FAILED in rows[0].flags
        assert FLAG_NO_VALID_SAMPLES in rows[0].flags
        assert rows[0].overloads == 2
        assert rows[0].verdict is Verdict.NO_SPEC
        assert "sentinel" in rows[0].reason


# --------------------------------------------------------------------------
# Raw samples and the meter's internal temperature
# --------------------------------------------------------------------------

class _ThermometerMeter(FakeMeter):
    """A meter offering the *optional* ``internal_temperature`` capability."""

    def __init__(self, *args, internal_temp_c=38.75, temp_error=None,
                 **kwargs):
        super().__init__(*args, **kwargs)
        self.internal_temp_c = internal_temp_c
        self.temp_error = temp_error
        self.temp_reads = 0

    def internal_temperature(self):
        self.temp_reads += 1
        if self.temp_error is not None:
            raise self.temp_error
        return self.internal_temp_c


class TestSamplesAndMeterTemperature:
    """The row carries the raw evidence and the meter's own temperature."""

    def test_the_row_carries_the_surviving_samples(self):
        samples = [10.0000012, 10.0000010, 9.9e37, 10.0000015]
        rows = run(make_plan(), FakeSource(),
                   FakeMeter(spec=load_model("hp3458a"), samples=samples))
        # The sentinel was excluded by Reading.from_samples and must not be
        # resurrected into the evidence for the mean.
        assert rows[0].samples == (10.0000012, 10.0000010, 10.0000015)
        assert rows[0].overloads == 1
        assert rows[0].n == 3

    def test_a_failed_point_keeps_whatever_samples_it_got(self):
        rows = run(make_plan(), FakeSource(),
                   FakeMeter(samples=[9.9e37, 9.9e37]))
        assert FLAG_POINT_FAILED in rows[0].flags
        assert rows[0].samples == ()

    def test_meter_temperature_is_recorded_per_point(self):
        points = [dcv_point(value=v) for v in (1.0, 2.0)]
        meter = _ThermometerMeter(internal_temp_c=38.75)
        rows = run(make_plan(points), FakeSource(), meter)
        assert [row.meter_temp_c for row in rows] == [38.75, 38.75]
        # Once per point, after the reading -- never a second channel of I/O.
        assert meter.temp_reads == 2

    def test_a_meter_without_the_capability_records_none(self):
        rows = run(make_plan(), FakeSource(), FakeMeter())
        assert rows[0].meter_temp_c is None

    def test_a_meter_answering_none_records_none(self):
        rows = run(make_plan(), FakeSource(),
                   _ThermometerMeter(internal_temp_c=None))
        assert rows[0].meter_temp_c is None

    def test_a_failed_temperature_read_costs_nothing(self):
        """Provenance must never take a measured point down."""
        meter = _ThermometerMeter(temp_error=RuntimeError("GPIB timeout"))
        rows = run(make_plan(), FakeSource(), meter)
        assert rows[0].meter_temp_c is None
        assert FLAG_POINT_FAILED not in rows[0].flags
        assert rows[0].verdict is not None

    def test_a_non_finite_answer_is_not_a_temperature(self):
        rows = run(make_plan(), FakeSource(),
                   _ThermometerMeter(internal_temp_c=math.nan))
        assert rows[0].meter_temp_c is None


# --------------------------------------------------------------------------
# Failure and health
# --------------------------------------------------------------------------

class TestFailureRule:
    """One bad point is a point; a trip or a cable is a run."""

    def test_a_failed_point_is_recorded_and_the_run_continues(self):
        points = [dcv_point(value=v, label=f"p{i}")
                  for i, v in enumerate((1.0, 2.0, 3.0))]
        meter = FakeMeter(fail_at=lambda p: abs(p.value - 2.0) < 0.5)
        source = FakeSource()
        rows = run(make_plan(points), source, meter)

        assert len(rows) == 3
        assert [FLAG_POINT_FAILED in r.flags for r in rows] == [False, True,
                                                                False]
        failed = rows[1]
        assert failed.verdict is Verdict.NO_SPEC
        assert "the meter could not be configured" in failed.reason
        assert failed.point_index == 1
        # The meter is configured *before* the source is applied (H03), so a
        # meter that cannot be configured is a point that was never sourced --
        # and the row says so rather than recording a setpoint that never was.
        assert failed.source_value is None
        # The wiring proof's 11 mV, then the two points that could be read.
        assert [p.value for p in source.applied] == [pytest.approx(0.011),
                                                     pytest.approx(1.0),
                                                     pytest.approx(3.0)]
        assert failed.timestamp is not None
        assert failed.mean is None

    def test_a_source_refusal_is_a_failed_point_not_a_dead_run(self):
        points = [dcv_point(value=v) for v in (1.0, 2.0)]
        source = FakeSource(fail_at=lambda p: p.value == 1.0)
        rows = run(make_plan(points), source, FakeMeter())
        assert FLAG_POINT_FAILED in rows[0].flags
        assert "refused" in rows[0].reason
        assert FLAG_POINT_FAILED not in rows[1].flags

    def test_enough_consecutive_failures_ends_the_run(self):
        points = [dcv_point(value=float(i + 1)) for i in range(10)]
        # Every planned point fails to configure; the wiring proof at 11 mV
        # is not a planned point and still goes through, so what this pins is
        # the consecutive-failure rule and not the proof.
        meter = FakeMeter(fail_at=lambda p: p.value >= 1.0)
        source = FakeSource()
        generator = run_plan(make_plan(points), source, meter,
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock(), max_consecutive_failures=3)
        rows = []
        with pytest.raises(EngineError, match="consecutive"):
            for row in generator:
                rows.append(row)
        # Every attempted point was recorded before the run gave up.
        assert len(rows) == 3
        assert all(FLAG_POINT_FAILED in r.flags for r in rows)
        assert source.standby_calls >= 1

    def test_a_failure_streak_is_reset_by_a_good_point(self):
        points = [dcv_point(value=float(i + 1)) for i in range(6)]
        # Fail on the odd values: never three in a row.
        meter = FakeMeter(fail_at=lambda p: int(p.value) % 2 == 1)
        rows = run(make_plan(points), FakeSource(), meter,
                   max_consecutive_failures=3)
        assert len(rows) == 6
        assert sum(FLAG_POINT_FAILED in r.flags for r in rows) == 3


class TestSourceHealth:
    """A trip or a queued fault stops the run; conditions are flagged."""

    def test_a_trip_mid_run_stops_the_run_and_says_where(self):
        points = [dcv_point(value=float(i + 1)) for i in range(4)]
        tripped = replace(HEALTHY, tripped=True, operate=False)
        # Two health reads per point -- before it and after the reading.
        source = FakeSource(health=[HEALTHY] * 4 + [tripped])
        generator = run_plan(make_plan(points), source, FakeMeter(),
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        rows = []
        with pytest.raises(SourceFaultError) as caught:
            for row in generator:
                rows.append(row)
        assert len(rows) == 2                       # the good ones survive
        assert caught.value.point_index == 2
        assert caught.value.group_index == 0
        assert caught.value.health.tripped
        assert "start_group=0" in str(caught.value)
        assert source.log[-1] == "standby"

    def test_a_trip_between_apply_and_read_is_caught_for_that_point(self):
        """M81: the row must not carry a verdict for a de-energized output.

        A trip after ``apply`` reads as a catastrophic error of the DUT --
        the meter sees an output that is no longer there -- and a health check
        taken only *before* the point cannot see it.
        """
        tripped = replace(HEALTHY, tripped=True, operate=False)
        source = FakeSource(health=[HEALTHY, tripped])
        generator = run_plan(make_plan(), source, FakeMeter(),
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        rows = []
        with pytest.raises(SourceFaultError, match="while point 0 was"):
            for row in generator:
                rows.append(row)
        assert len(rows) == 1
        assert FLAG_POINT_FAILED in rows[0].flags
        assert FLAG_SOURCE_TRIPPED_MID_POINT in rows[0].flags
        assert rows[0].verdict is Verdict.NO_SPEC
        assert rows[0].tolerance_limit is None
        assert source.log[-1] == "standby"


    def test_a_queued_fault_stops_the_run(self):
        faulted = replace(HEALTHY, faults=((504, "Output overloaded"),))
        source = FakeSource(health=faulted)
        with pytest.raises(SourceFaultError, match="Output overloaded"):
            run(make_plan(), source, FakeMeter())
        assert source.log[-1] == "standby"

    def test_a_cold_calibrator_is_flagged_on_every_row(self):
        source = FakeSource(health=replace(HEALTHY, warm_minutes=5))
        row = run(make_plan(), source)[0]
        assert FLAG_SOURCE_NOT_WARM in row.flags

    def test_overdue_zeros_are_flagged(self):
        source = FakeSource(health=replace(HEALTHY, days_since_zero=45))
        row = run(make_plan(), source)[0]
        assert FLAG_SOURCE_ZEROS_DUE in row.flags


class TestConditionPolicy:
    """M65: the reference's own prerequisites gate the claim, both ways.

    Warm-up and the 30-day dc zeros are conditions of the 5700A's
    specifications (OM 1996 pp. 29-30), not house rules.  They used to be
    flag-only, so a PASS could be issued on a cold or un-zeroed reference and
    nothing in the report said so.
    """

    def test_a_cold_calibrator_cannot_pass(self):
        source = FakeSource(health=replace(HEALTHY, warm_minutes=5))
        row = run(make_plan(), source)[0]
        assert row.verdict is Verdict.INDETERMINATE
        assert FLAG_SOURCE_NOT_WARM in row.flags
        assert FLAG_CONDITIONS_NOT_MET in row.flags
        assert "warm for 5 min" in row.reason

    def test_overdue_zeros_cannot_pass(self):
        source = FakeSource(health=replace(HEALTHY, days_since_zero=45))
        row = run(make_plan(), source)[0]
        assert row.verdict is Verdict.INDETERMINATE
        assert FLAG_CONDITIONS_NOT_MET in row.flags
        assert "45 days old" in row.reason

    def test_a_condition_blocks_a_failure_too(self):
        """Never a counted FAIL: the specification is not in force here.

        800 uV is twice the 34401A's 1-year limit at 10 V and well inside
        the ten tolerances §8's gross-error rule abandons a run over.
        """
        source = FakeSource(health=replace(HEALTHY, warm_minutes=5))
        row = run(make_plan(), source, FakeMeter(error=+8e-4))[0]
        assert abs(row.error) > row.tolerance_limit      # it would have failed
        assert row.verdict is Verdict.INDETERMINATE
        assert "neither conformity nor non-conformity" in row.reason

    def test_the_conditions_are_their_own_bucket(self):
        source = FakeSource(health=replace(HEALTHY, warm_minutes=5))
        rows = run(make_plan(), source)
        summary = summarize_run(rows)
        assert summary[Verdict.PASS] == 0
        assert summary[Verdict.FAIL] == 0
        assert summary.indeterminate_conditions == 1
        assert summary.indeterminate_guard_band == 0
        assert summary.indeterminate_temperature == 0
        assert "outside other stated conditions" in summary.line()


    def test_an_output_that_never_came_on_is_flagged(self):
        source = FakeSource(operate=False)
        row = run(make_plan(), source)[0]
        assert FLAG_SOURCE_NOT_OPERATE in row.flags
        assert row.source_operate is False


# --------------------------------------------------------------------------
# Groups, resumption and standby
# --------------------------------------------------------------------------

def three_group_plan():
    """DCV, then DCI, then 4-wire RES: three rewiring groups."""
    points = [
        dcv_point(value=1.0, label="dcv-1"),
        dcv_point(value=10.0, label="dcv-2"),
        PlanPoint(quantity=Quantity.DCI, value=0.01,
                  connection=Connection.TWO_WIRE, range_arg=0.1,
                  range_id="DC22MA", interval="1y", label="dci-1"),
        PlanPoint(quantity=Quantity.RES, value=10.0,
                  connection=Connection.FOUR_WIRE, range_id="OHM10",
                  range_arg=100.0, interval="1y", label="res-1"),
    ]
    return make_plan(points)


class TestGroups:
    """Boundaries are where the operator re-cables -- and where a run resumes."""

    def test_the_plan_groups_as_expected(self):
        groups = three_group_plan().groups()
        assert [g.start for g in groups] == [0, 2, 3]
        assert [len(g) for g in groups] == [2, 1, 1]

    def test_a_boundary_is_signalled_before_each_group(self):
        seen = []
        rows = run(three_group_plan(), on_group=seen.append)
        assert [b.index for b in seen] == [0, 1, 2]
        assert [b.start for b in seen] == [0, 2, 3]
        assert [b.points for b in seen] == [2, 1, 1]
        assert all(b.total == 3 for b in seen)
        assert seen[2].quantity is Quantity.RES
        assert seen[2].connection is Connection.FOUR_WIRE
        assert "group 3/3" in seen[2].describe()
        # A complete run skipped nothing -- at every boundary, including the
        # last.  ``skipped`` is where the run began, not how far it has got.
        assert [b.skipped for b in seen] == [0, 0, 0]
        assert [r.group_index for r in rows] == [0, 0, 1, 2]
        assert [r.point_index for r in rows] == [0, 1, 2, 3]

    def test_the_source_is_in_standby_when_the_operator_is_prompted(self):
        states = []
        source = FakeSource()

        def prompt(boundary):
            # The last thing the source was told, before any prompt, is standby.
            states.append(source.log[-1])

        run(three_group_plan(), source, on_group=prompt)
        assert states == ["standby", "standby", "standby"]

    def test_starting_from_a_group_index_skips_the_earlier_ones(self):
        seen = []
        rows = run(three_group_plan(), start_group=1, on_group=seen.append)
        assert [b.index for b in seen] == [1, 2]
        assert [r.label for r in rows] == ["dci-1", "res-1"]
        assert [r.point_index for r in rows] == [2, 3]
        # Both boundaries report the two points this run never attempted.
        assert [b.skipped for b in seen] == [2, 2]

    def test_resuming_past_the_end_yields_nothing(self):
        assert run(three_group_plan(), start_group=3) == []

    def test_an_impossible_start_group_is_refused_at_the_desk(self):
        with pytest.raises(EngineError, match="start_group"):
            run_plan(three_group_plan(), FakeSource(), FakeMeter(),
                     start_group=9)
        with pytest.raises(EngineError, match="start_group"):
            run_plan(three_group_plan(), FakeSource(), FakeMeter(),
                     start_group=-1)

    def test_a_prompt_that_raises_leaves_the_source_in_standby(self):
        source = FakeSource()

        def refuse(boundary):
            raise KeyboardInterrupt("the operator stopped")

        with pytest.raises(KeyboardInterrupt):
            run(three_group_plan(), source, on_group=refuse)
        assert source.log[-1] == "standby"


class TestStartPoint:
    """Entering a group part-way -- what a resume inside a group needs.

    Without it a run interrupted mid-group has only two options and both are
    wrong: re-measure the points it already has (duplicate rows, which
    double-count in every summary and on a certificate) or skip to the next
    group (points silently never taken).
    """

    def test_it_skips_the_points_already_measured(self):
        seen = []
        rows = run(three_group_plan(), start_group=0, start_point=1,
                   on_group=seen.append)
        assert [r.point_index for r in rows] == [1, 2, 3]
        assert [b.index for b in seen] == [0, 1, 2]

    def test_only_the_resumed_group_starts_part_way(self):
        rows = run(three_group_plan(), start_group=0, start_point=1)
        # The first point of group 0 is skipped; groups 1 and 2 still start at
        # their own first point.
        assert [r.label for r in rows] == ["dcv-2", "dci-1", "res-1"]

    def test_the_operator_is_still_prompted_for_the_resumed_group(self):
        """The wiring belongs to the group, not to where in it a run resumes."""
        states = []
        source = FakeSource()
        seen = []

        def prompt(boundary):
            states.append(source.log[-1])
            seen.append(boundary)

        run(three_group_plan(), source, start_group=0, start_point=1,
            on_group=prompt)
        assert states[0] == "standby"
        assert seen[0].index == 0
        assert seen[0].offset == 1
        assert seen[0].points == 2 and seen[0].remaining == 1
        assert "resuming at #1" in seen[0].describe()
        # Every later group is entered whole.
        assert [b.offset for b in seen] == [1, 0, 0]
        assert [b.remaining for b in seen] == [1, 1, 1]

    def test_what_the_run_never_attempted_counts_the_offset(self):
        seen = []
        run(three_group_plan(), start_group=1, start_point=1,
            on_group=seen.append)
        # Group 1 has one point, so an offset of 1 is its end: three points of
        # the plan are never attempted by this run.
        assert [b.skipped for b in seen] == [3, 3]

    def test_an_offset_past_the_end_of_its_group_is_refused_at_the_desk(self):
        with pytest.raises(EngineError, match="past the end of group"):
            run_plan(three_group_plan(), FakeSource(), FakeMeter(),
                     start_group=0, start_point=3)

    def test_a_negative_offset_is_refused(self):
        with pytest.raises(EngineError, match="start_point"):
            run_plan(three_group_plan(), FakeSource(), FakeMeter(),
                     start_point=-1)

    def test_an_offset_with_no_group_to_index_is_refused(self):
        with pytest.raises(EngineError, match="no group"):
            run_plan(three_group_plan(), FakeSource(), FakeMeter(),
                     start_group=3, start_point=1)

    def test_the_default_changes_nothing(self):
        assert [r.point_index for r in run(three_group_plan())] == [0, 1, 2, 3]


class _ZeroingSource(FakeSource):
    """A source offering the optional ``zero_output`` capability (M52).

    The 5700A's own procedure before anything is re-cabled is ``0 V ENTER``
    *and* standby, not standby alone: standby leaves the last setpoint -- up
    to 1100 V -- selected, and one OPR press re-applies it.
    """

    def __init__(self, *args, zero_error=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.zero_error = zero_error
        self.zero_calls = 0

    def zero_output(self):
        self.log.append("zero")
        self.zero_calls += 1
        if self.zero_error is not None:
            raise self.zero_error


class TestZeroBeforeStandby:
    """M52: no boundary and no shutdown leaves 1100 V programmed."""

    def test_the_setpoint_is_zeroed_before_every_boundary_and_at_the_end(self):
        source = _ZeroingSource()
        run(three_group_plan(), source)
        # Three boundaries and the shutdown, each zero-then-standby.
        pairs = [(a, b) for a, b in zip(source.log, source.log[1:])
                 if a == "zero"]
        assert pairs == [("zero", "standby")] * 4
        assert source.zero_calls == 4
        assert source.log[-2:] == ["zero", "standby"]

    def test_the_operator_is_prompted_only_after_the_zero_and_the_standby(self):
        source = _ZeroingSource()
        seen = []
        run(three_group_plan(), source,
            on_group=lambda b: seen.append(source.log[-2:]))
        assert seen == [["zero", "standby"]] * 3

    def test_a_source_without_the_capability_still_runs(self):
        source = FakeSource()
        run(three_group_plan(), source)
        assert source.log[-1] == "standby"

    def test_a_failed_zero_still_de_energizes_and_tells_the_operator(self):
        """Standby is what protects the operator; the zero is belt and braces."""
        source = _ZeroingSource(zero_error=RuntimeError("GPIB timeout"))
        seen = []
        rows = run(three_group_plan(), source, on_group=seen.append)
        assert len(rows) == 4
        assert source.log[-1] == "standby"
        assert "GPIB timeout" in seen[0].zero_error
        assert "could not be programmed to zero" in seen[0].describe()

    def test_a_failed_zero_on_the_abort_path_reaches_the_abort_message(self):
        """The abort path had nowhere to put it and dropped it on the floor.

        A group boundary hands the operator a ``GroupBoundary`` that carries
        ``zero_error``; an abort hands them an exception and nothing else, so
        the one place the words can go is the exception's own message -- and
        "the output is in standby but 1000 V may still be selected" is not a
        detail to lose while telling somebody to check their wiring.
        """
        source = _ZeroingSource(zero_error=RuntimeError("GPIB timeout"))
        meter = _ErrorAtMeter(at_value=10.0, gross=1e-2)
        meter.source = source
        generator = run_plan(make_plan([dcv_point(value=10.0, label="p0")]),
                             source, meter, environment=FakeProbe(),
                             sleep=lambda s: None, clock=FakeClock())
        rows = []
        with pytest.raises(SafetyAbortError) as excinfo:
            for row in generator:
                rows.append(row)
        assert FLAG_GROSS_ERROR_ABORT in rows[0].flags
        assert "GPIB timeout" in str(excinfo.value)
        assert "still be *selected*" in str(excinfo.value)
        # The rule's own words and its resume information survive.
        assert "that is a connection" in str(excinfo.value)
        assert excinfo.value.flag == FLAG_GROSS_ERROR_ABORT


class TestStandby:
    """However the iteration ends, the output ends de-energized."""

    def test_closing_the_generator_leaves_the_source_in_standby(self):
        source = FakeSource()
        generator = run_plan(three_group_plan(), source, FakeMeter(),
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        next(generator)
        before = source.standby_calls
        generator.close()
        assert source.standby_calls == before + 1
        assert source.log[-1] == "standby"

    def test_a_keyboard_interrupt_leaves_the_source_in_standby(self):
        source = FakeSource()
        generator = run_plan(three_group_plan(), source, FakeMeter(),
                             environment=FakeProbe(), sleep=lambda s: None,
                             clock=FakeClock())
        next(generator)
        with pytest.raises(KeyboardInterrupt):
            generator.throw(KeyboardInterrupt())
        assert source.log[-1] == "standby"

    def test_breaking_out_of_the_loop_leaves_the_source_in_standby(self):
        source = FakeSource()
        for _ in run_plan(three_group_plan(), source, FakeMeter(),
                          environment=FakeProbe(), sleep=lambda s: None,
                          clock=FakeClock()):
            break
        # The generator is collected/closed at the end of the loop statement.
        assert source.log[-1] == "standby"

    def test_a_complete_run_ends_in_standby(self):
        source = FakeSource()
        run(three_group_plan(), source)
        assert source.log[-1] == "standby"

    def test_a_standby_that_did_not_take_effect_is_never_swallowed(self):
        """The adapter verifies STBY (M47); a live output is not a clean stop.

        ``Fluke5700ASource.standby()`` reads the ISR back and raises when the
        OPER bit is still set.  Coming out of a ``finally`` that is unwinding
        something else, that exception must still reach the operator -- it is
        the one failure mode this module is arranged to prevent.
        """
        class _ObstinateSource(FakeSource):
            def standby(self):
                super().standby()
                raise RuntimeError("STBY sent but the OPER bit is still set")

        source = _ObstinateSource()
        with pytest.raises(EngineError, match="MAY STILL BE LIVE"):
            run(make_plan(), source)
        assert source.standby_calls >= 1               # it was attempted


# --------------------------------------------------------------------------
# The whole way through the file
# --------------------------------------------------------------------------

class TestResultFileRoundTrip:
    """What the engine yields is what the result file was built to hold."""


    def test_a_streamed_run_is_readable_after_every_point(self, tmp_path):
        from caldmm.cal.result import open_results

        path = str(tmp_path / "stream.csv")
        header = RunHeader(run_id="stream", dut_role="meter")
        written = 0
        with open_results(path, header) as out:
            for row in run_plan(three_group_plan(), FakeSource(), FakeMeter(),
                                environment=FakeProbe(),
                                sleep=lambda s: None, clock=FakeClock()):
                out.write(row)
                written += 1
                # The file on disk is a complete record of the run so far.
                _, so_far = load_results(path)
                assert len(so_far) == written
        assert written == 4

    def test_resuming_appends_to_the_same_file(self, tmp_path):
        from caldmm.cal.result import open_results

        path = str(tmp_path / "resume.csv")
        header = RunHeader(run_id="resume", dut_role="meter")
        plan = three_group_plan()
        with open_results(path, header) as out:
            for row in run(plan)[:2]:
                out.write(row)
        with open_results(path, header, append=True) as out:
            for row in run(plan, start_group=1):
                out.write(row)
        _, rows = load_results(path)
        assert [r.point_index for r in rows] == [0, 1, 2, 3]


# -------------------------------------------------------------- invariants

IMPORT_HYGIENE_SCRIPT = textwrap.dedent("""\
    import sys

    BLOCKED = {"serial", "Gpib", "pyvisa", "smbus", "smbus2"}

    for name in list(sys.modules):
        if name.split(".")[0] in BLOCKED:
            del sys.modules[name]

    class Blocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in BLOCKED:
                raise ImportError("blocked for this test: " + fullname)
            return None

    sys.meta_path.insert(0, Blocker())

    import caldmm.cal.engine as engine

    # The engine is usable, not merely importable: a plan, two fake roles and
    # a full point, with no hardware library anywhere in the process.
    from caldmm.cal.plan import PlanPoint, TestPlan
    from caldmm.cal.quantities import Connection, Quantity
    from caldmm.cal.roles import (
        Environment, MeterConfig, Reading, SourceHealth, SourceState,
        Uncertainty)

    class Source:
        model_id = "fluke5700a"
        def validate(self, point): return []
        def apply(self, point):
            return SourceState(actual_value=10.0, unit="V", settled=True,
                               operate=True,
                               uncertainty=Uncertainty(5.0, "PPM", 0.95, 365))
        def standby(self): pass
        def health(self): return SourceHealth(operate=True, warm_minutes=45,
                                              days_since_zero=1)

    class Meter:
        model_id = "hp3458a"
        def supports(self, quantity, connection): return True
        def configure(self, point):
            return MeterConfig(func="DCV", range_arg=10.0, range_fs=10.0,
                               nplc=100.0, resolution=1e-5, settle_s=1.0)
        def read(self, n, discard=0):
            return Reading.from_samples([10.000001, 10.000002] * n,
                                        resolution=1e-7, unit="V")

    class Probe:
        def read(self): return Environment(ambient_c=23.0, source="fixed")

    point = PlanPoint(quantity=Quantity.DCV, value=10.0, range_arg=10.0,
                      range_id="DC11V", interval="1y")
    plan = TestPlan(dut_role="meter", source_model_id="fluke5700a",
                    meter_model_id="hp3458a", points=(point,), interval="1y")
    rows = list(engine.run_plan(plan, Source(), Meter(), environment=Probe(),
                                sleep=lambda s: None))
    assert len(rows) == 1, rows
    # A 5700A is not 4:1 better than a 3458A at 10 V, so a reading this
    # close to nominal is guarded into INDETERMINATE rather than a PASS;
    # what matters here is that the engine reached a verdict at all.
    assert rows[0].verdict.value in ("pass", "indeterminate"), rows[0].reason
    # Four terms, not the three this was written against: the loop's thermal
    # EMF is now carried as a *declared* 1 uV bound on every dc-volts point
    # unless --thermal-emf says otherwise, so it is in every budget the
    # engine builds (repeatability, reference, resolution, thermal emf).
    assert len(rows[0].budget) == 4, rows[0].budget
    assert [c.label for c in rows[0].budget][-1] == "thermal emf", \\
        rows[0].budget

    leaked = sorted(m for m in sys.modules if m.startswith("caldmm.drivers"))
    assert not leaked, "cal.engine pulled in drivers: " + repr(leaked)
    assert "caldmm.cal.adapters" not in sys.modules, \\
        "cal.engine imported adapters"
    print("OK")
    """)


def test_engine_imports_and_runs_with_no_hardware_libraries():
    """The architectural invariant: concrete roles are handed *in*.

    A subprocess with a real ``sys.meta_path`` blocker, so the root conftest's
    mock stubs are not in the way -- deleting ``sys.modules`` entries would not
    prove anything, because the import would simply succeed again.

    ``PYTHONPATH`` points the child at *the tree this test is running from*.
    Without it the child resolves ``caldmm`` through the editable install's
    ``.pth``, which is the main checkout -- so in a git worktree the invariant
    was being proved about somebody else's source.
    """
    package_root = os.path.dirname(os.path.dirname(os.path.abspath(
        caldmm.__file__)))
    env = dict(os.environ, PYTHONPATH=package_root)
    completed = subprocess.run([sys.executable, "-c", IMPORT_HYGIENE_SCRIPT],
                               capture_output=True, text=True, env=env)
    assert completed.returncode == 0, completed.stderr
    assert "OK" in completed.stdout


# --------------------------------------------------------------------------
# The planned range versus the range the calibrator was really on (H05)
# --------------------------------------------------------------------------

class RangeShiftingSource(FakeSource):
    """A calibrator that reports a different range from the planned one.

    What a real 5700A does with an unlocked range: 110 V pinned to DC1100V is
    sourced on DC220V, and ``RANGE?`` says so.
    """

    def __init__(self, reported, **kwargs):
        super().__init__(**kwargs)
        self._reported = reported

    def apply(self, point):
        return replace(super().apply(point), range_id=self._reported)


class TestSourceRangeMismatch:
    """H05: the readback is the authority, and a disagreement is not silent."""

    POINT = PlanPoint(quantity=Quantity.DCV, value=110.0,
                      connection=Connection.TWO_WIRE, range_id="DC1100V",
                      interval="1y", label="110 V on the 1100 V range")

    def _row(self, reported):
        plan = make_plan([self.POINT], dut_role="source",
                         meter_model="hp3458a")
        return run(plan, RangeShiftingSource(reported),
                   FakeMeter(load_model("hp3458a")))[0]

    def test_a_range_that_disagrees_with_the_plan_is_flagged(self):
        row = self._row("DC220V")
        assert FLAG_RANGE_MISMATCH in row.flags

    def test_the_limit_comes_from_the_range_the_output_was_on(self):
        """Not from the plan's row: the calibrator's 220 V range is a factor
        of five tighter at 110 V than its 1100 V range, and crediting the
        wrong one is what the flag exists to stop."""
        from caldmm.cal.specs import evaluate_limit

        source_spec = load_model("fluke5700a")
        actual = evaluate_limit(source_spec, Quantity.DCV, 110.0,
                                range_fs=220.0, interval="1y")
        planned = evaluate_limit(source_spec, Quantity.DCV, 110.0,
                                 range_fs=1100.0, interval="1y")
        assert actual.limit < planned.limit, "the hazard has moved"
        assert self._row("DC220V").spec_limit == pytest.approx(actual.limit)

    def test_agreement_is_not_flagged(self):
        from caldmm.cal.specs import evaluate_limit

        row = self._row("DC1100V")
        assert FLAG_RANGE_MISMATCH not in row.flags
        assert row.spec_limit == pytest.approx(
            evaluate_limit(load_model("fluke5700a"), Quantity.DCV, 110.0,
                           range_fs=1100.0, interval="1y").limit)

    def test_a_range_name_the_model_file_does_not_know_is_not_a_mismatch(self):
        """``RANGE?`` is the instrument's vocabulary, and a firmware that
        answers something the file never listed is a gap in the file, not a
        disagreement about which range the output was on."""
        row = self._row("DC5725V")
        assert FLAG_RANGE_MISMATCH not in row.flags


# --------------------------------------------------------------------------
# Early termination of sampling (2026-11-01)
# --------------------------------------------------------------------------

from caldmm.cal.engine import (  # noqa: E402
    FLAG_SAMPLING_NOT_CONVERGED,
    FLAG_SAMPLING_STOPPED_EARLY,
    sampling_converged,
)


class TestSamplingConverged:
    """The pure stopping rule.  **No I/O.**"""

    U_B = 1e-5                 # Type B standard uncertainty, absolute

    def test_three_quiet_samples_converge(self):
        ok, why = sampling_converged([10.0, 10.0000001, 9.9999999],
                                     u_b=self.U_B, planned=10)
        assert ok, why

    def test_fewer_than_three_never_converge(self):
        ok, _ = sampling_converged([10.0, 10.0], u_b=self.U_B)
        assert not ok

    def test_spread_too_large_against_u_b(self):
        # sd 1e-5 over 3 samples -> sem ~5.8e-6, more than 0.3 x 1e-5.
        ok, why = sampling_converged([10.0, 10.00001, 9.99999],
                                     u_b=self.U_B, planned=10)
        assert not ok and "spread" in why

    def test_spread_inside_u_b_converges(self):
        # sem ~2.3e-6 < 3e-6: the Type A term grows u_c by < 4.4 %.
        ok, why = sampling_converged([10.0, 10.000004, 9.999996],
                                     u_b=self.U_B, planned=3)
        assert ok, why

    def test_a_ramp_is_caught_by_its_projection(self):
        # 2026-11-01, 675 V ac: a steady ramp.  1e-6 per sample: three
        # samples are quiet (sem 0.58e-6), but with 7 left the full block's
        # mean would move 3.5e-6 > 0.2 x 1e-5.
        samples = [10.0 + 1e-6 * i for i in range(3)]
        ok, why = sampling_converged(samples, u_b=self.U_B, planned=10)
        assert not ok and "trend" in why

    def test_the_same_ramp_at_the_full_count_projects_nothing(self):
        samples = [10.0 + 1e-6 * i for i in range(3)]
        assert sampling_converged(samples, u_b=self.U_B, planned=3)[0]
        assert sampling_converged(samples, u_b=self.U_B)[0]

    def test_near_the_limit_the_rule_tightens(self):
        samples = [9.999996, 10.000004, 9.999996]      # sem 2.7e-6, flat
        far = sampling_converged(samples, u_b=self.U_B, planned=10,
                                 error=0.5e-4, dut_limit=1e-4)
        near = sampling_converged(samples, u_b=self.U_B, planned=10,
                                  error=0.9e-4, dut_limit=1e-4)
        assert far[0], far[1]
        assert not near[0] and "near the limit" in near[1]

    def test_near_the_limit_a_quiet_block_still_stops(self):
        # The first rule refused *every* point past 80 % of its limit; on the
        # 34401A's ACV ranges that was 13 points whose sem was 1 % of u_B.
        ok, why = sampling_converged([10.0, 10.0000001, 9.9999999],
                                     u_b=self.U_B, planned=10,
                                     error=0.95e-4, dut_limit=1e-4)
        assert ok, why

    def test_an_unparseable_limit_counts_as_near(self):
        ok, why = sampling_converged([9.999996, 10.000004, 9.999996],
                                     u_b=self.U_B, planned=10,
                                     error="x", dut_limit=1e-4)
        assert not ok and "near the limit" in why

    @pytest.mark.parametrize("u_b", [None, 0.0, math.nan, -1.0])
    def test_no_usable_u_b_never_converges(self, u_b):
        ok, _ = sampling_converged([10.0, 10.0, 10.0], u_b=u_b)
        assert not ok


class _SequenceMeter(FakeMeter):
    """Hands out samples from one stream across however many reads happen."""

    def __init__(self, stream, **kwargs):
        super().__init__(**kwargs)
        self._stream = list(stream)

    def _read(self, n, discard):
        if self._proof:
            return super()._read(n, discard)
        self.log.append("read")
        self.reads.append((n, discard))
        take, self._stream = (self._stream[:n + discard],
                              self._stream[n + discard:])
        return Reading.from_samples(
            take[discard:], overload=self.spec.is_overload,
            discarded=discard, resolution=self._config.resolution,
            unit=self._quantity.unit if self._quantity else "")


class TestEarlyStopInTheEngine:

    def _plan(self):
        return make_plan(samples=10, discards=1)

    def test_off_by_default_one_read_of_the_full_count(self):
        meter = _SequenceMeter([10.0] * 11)
        rows = run(self._plan(), FakeSource(), meter)
        assert [r for r in meter.reads if r != (1, 0)][-1] == (10, 1)
        assert rows[0].n == 10
        assert FLAG_SAMPLING_STOPPED_EARLY not in rows[0].flags

    def test_stable_readings_stop_at_three(self):
        meter = _SequenceMeter([10.0] * 11)
        rows = run(self._plan(), FakeSource(), meter, min_samples=3)
        assert meter.reads[-1] == (3, 1)
        assert rows[0].n == 3
        assert rows[0].discarded == 1
        assert FLAG_SAMPLING_STOPPED_EARLY in rows[0].flags

    def test_a_heating_reading_runs_to_the_maximum(self):
        stream = [10.0] + [10.0 + 2e-5 * i for i in range(10)]
        meter = _SequenceMeter(stream)
        rows = run(self._plan(), FakeSource(), meter, min_samples=3)
        assert rows[0].n == 10
        assert rows[0].samples == tuple(stream[1:])
        assert FLAG_SAMPLING_NOT_CONVERGED in rows[0].flags
        assert FLAG_SAMPLING_STOPPED_EARLY not in rows[0].flags

    def test_a_transient_then_quiet_stops_once_it_settles(self):
        # Noisy start, then quiet: the rule extends one sample at a time and
        # stops as soon as the whole block satisfies it -- not before.
        stream = [10.0, 10.0, 10.00003, 10.0] + [10.0] * 7
        meter = _SequenceMeter(stream)
        rows = run(self._plan(), FakeSource(), meter, min_samples=3)
        assert 3 < rows[0].n <= 10

    def test_an_overload_disables_the_early_stop(self):
        stream = [10.0, 10.0, 9.9e37, 10.0] + [10.0] * 7
        meter = _SequenceMeter(stream)
        rows = run(self._plan(), FakeSource(), meter, min_samples=3)
        assert rows[0].n + rows[0].overloads == 10
        assert FLAG_SAMPLING_STOPPED_EARLY not in rows[0].flags

    def test_min_samples_at_the_plan_count_is_the_old_behaviour(self):
        meter = _SequenceMeter([10.0] * 11)
        rows = run(self._plan(), FakeSource(), meter, min_samples=10)
        assert rows[0].n == 10
        assert FLAG_SAMPLING_STOPPED_EARLY not in rows[0].flags


class TestProbesAreReadFast:
    """The wiring proof and the pre-step use configure_probe when offered."""

    def test_proof_and_prestep_go_through_configure_probe(self):
        calls = []

        class _ProbeMeter(_RangingMeter):
            def configure_probe(self, point):
                calls.append(float(point.value))
                return self.configure(point)

        point = dci_point(1.0, range_id="DC2_2A")
        plan = make_plan([point], dut_role="source", meter_model="hp3458a")
        meter = _ProbeMeter(load_model("hp3458a"))
        run(plan, FakeSource(), meter)
        # The proof (2.2 mA) and the pre-step, configured from the nominal.
        assert calls == [pytest.approx(2.2e-3), pytest.approx(1.0)]
        # ...and the point itself was configured the ordinary way last.
        assert meter.configured[-1].value == pytest.approx(1.0)

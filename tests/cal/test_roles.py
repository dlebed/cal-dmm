"""Tests for caldmm.cal.roles — the value objects and the Protocol seam.

Offline, and pointedly so: this module is the one the whole engine sits on, and
its only project import is ``cal.quantities``.  The import-hygiene test at the
bottom pins that in a clean interpreter with the hardware libraries blocked.
"""

import math
import subprocess
import sys
import textwrap

import pytest

from caldmm.cal.quantities import Connection, Quantity
from caldmm.cal.roles import (
    UNKNOWN_ENVIRONMENT,
    Environment,
    EnvironmentProbe,
    MeterConfig,
    MeterRole,
    Point,
    PointLike,
    Reading,
    SourceHealth,
    SourceRole,
    SourceState,
    Uncertainty,
)


def _protocol_members(protocol) -> "set[str]":
    """The names a ``runtime_checkable`` Protocol makes mandatory.

    ``__protocol_attrs__`` is a CPython 3.12 addition; Python 3.11 is the minimum supported version,
    where the same set lives behind ``typing._get_protocol_attrs``.  Both are
    implementation details, so this asks for whichever one exists rather than
    pinning the test suite to one interpreter version.
    """
    attrs = getattr(protocol, "__protocol_attrs__", None)
    if attrs is None:
        import typing
        attrs = typing._get_protocol_attrs(protocol)
    return set(attrs)


# ------------------------------------------------------------- Uncertainty

class TestUncertainty:
    def test_absolute_unit_is_returned_as_is(self):
        assert Uncertainty(3.5e-6, "V").absolute(10.0) == pytest.approx(3.5e-6)

    def test_ppm_is_taken_against_the_reading(self):
        # 5 ppm of 10 V is 50 uV, not 5 uV and not 5e-6 of anything else.
        assert Uncertainty(5.0, "PPM").absolute(10.0) == pytest.approx(50e-6)

    def test_percent_is_taken_against_the_reading(self):
        assert Uncertainty(0.01, "PCT").absolute(100.0) == pytest.approx(0.01)
        assert Uncertainty(0.01, "%").absolute(100.0) == pytest.approx(0.01)

    def test_relative_flag(self):
        assert Uncertainty(5.0, "ppm").is_relative
        assert not Uncertainty(5.0, "V").is_relative

    def test_negative_reading_uses_magnitude(self):
        assert Uncertainty(5.0, "PPM").absolute(-10.0) == pytest.approx(50e-6)

    def test_unknown_unit_falls_back_to_absolute(self):
        # The 5700A's UNCERT? unit list is explicitly open-ended; an
        # unrecognised one must not be silently scaled by the reading.
        assert Uncertainty(2.0, "OHM").absolute(19000.0) == 2.0


# -------------------------------------------------------------- SourceState

class TestSourceState:
    def test_expanded_uncertainty_is_absolute(self):
        state = SourceState(actual_value=10.0, unit="V",
                            uncertainty=Uncertainty(5.0, "PPM", 0.95, 365))
        assert state.expanded_uncertainty == pytest.approx(50e-6)

    def test_no_uncertainty_is_none_not_zero(self):
        # The 5700A's -1.0 sentinel means "no specification for this point".
        # Zero would claim a perfect reference.
        assert SourceState(actual_value=10.0, unit="V").expanded_uncertainty is None

    def test_defaults_are_not_energized(self):
        state = SourceState(actual_value=0.0, unit="V")
        assert not state.operate and not state.settled

    def test_is_frozen(self):
        state = SourceState(actual_value=10.0, unit="V")
        with pytest.raises(Exception):
            state.actual_value = 11.0


# ------------------------------------------------------------------ Reading

class TestReading:
    def test_plain_statistics(self):
        reading = Reading.from_samples([1.0, 2.0, 3.0])
        assert reading.n == 3
        assert reading.mean == pytest.approx(2.0)
        assert reading.stdev == pytest.approx(1.0)
        assert reading.overloads == 0
        assert reading.samples == (1.0, 2.0, 3.0)

    def test_overloads_are_counted_and_excluded_from_the_mean(self):
        reading = Reading.from_samples(
            [10.0, 1e35, 12.0], overload=lambda v: abs(v) >= 1e35)
        assert reading.overloads == 1
        assert reading.n == 2
        assert reading.mean == pytest.approx(11.0)
        assert 1e35 not in reading.samples

    def test_default_predicate_drops_non_finite(self):
        reading = Reading.from_samples([1.0, float("nan"), float("inf"), 3.0])
        assert reading.overloads == 2
        assert reading.mean == pytest.approx(2.0)

    def test_stdev_of_one_sample_is_nan_not_zero(self):
        reading = Reading.from_samples([5.0])
        assert reading.n == 1
        assert math.isnan(reading.stdev)
        assert math.isnan(reading.stdev_mean)

    def test_all_overloaded_gives_nan_mean(self):
        reading = Reading.from_samples([1e35, 1e35], overload=lambda v: True)
        assert reading.n == 0
        assert reading.overloads == 2
        assert math.isnan(reading.mean)

    def test_stdev_mean_is_stdev_over_root_n(self):
        reading = Reading.from_samples([1.0, 2.0, 3.0, 4.0])
        assert reading.stdev_mean == pytest.approx(reading.stdev / 2.0)

    def test_carries_resolution_and_discards(self):
        reading = Reading.from_samples([1.0, 2.0], discarded=2,
                                       resolution=1e-7, unit="V")
        assert (reading.discarded, reading.resolution, reading.unit) == \
            (2, 1e-7, "V")


# --------------------------------------------------------------- MeterConfig

def test_meter_config_defaults_are_inert():
    config = MeterConfig()
    assert config.func is None and config.settle_s == 0.0
    assert dict(config.applied) == {}


# --------------------------------------------------------------- SourceHealth

def test_source_health_defaults_claim_nothing():
    health = SourceHealth()
    assert health.faults == ()
    assert health.warm_minutes is None
    assert health.days_since_zero is None


# ---------------------------------------------------------------- Environment

class TestEnvironment:
    def test_unknown_by_default(self):
        assert UNKNOWN_ENVIRONMENT.ambient_c is None
        assert not UNKNOWN_ENVIRONMENT.ambient_known
        assert UNKNOWN_ENVIRONMENT.ambient_for_verdict is None

    def test_known_value_passes_through(self):
        env = Environment(ambient_c=23.1, source="probe")
        assert env.ambient_known
        assert env.ambient_for_verdict == pytest.approx(23.1)

    def test_nan_is_unknown_not_out_of_window(self):
        # A failed sensor read is the commonest way a nan arrives; every
        # ordered comparison against it is False, so a naive window check
        # would call it out-of-window and print a temperature nobody measured.
        env = Environment(ambient_c=float("nan"))
        assert not env.ambient_known
        assert env.ambient_for_verdict is None


# ---------------------------------------------------------------------- Point

class TestPoint:
    def test_unit_comes_from_the_quantity(self):
        assert Point(Quantity.DCV, 10.0).unit == "V"
        assert Point(Quantity.RES, 10e3).unit == "OHM"
        assert Point(Quantity.DCI, 1.0).unit == "A"

    def test_defaults(self):
        point = Point(Quantity.DCV, 10.0)
        assert point.frequency_hz == 0.0
        assert point.connection is Connection.TWO_WIRE
        assert point.mode is None and point.range_id is None

    def test_satisfies_pointlike(self):
        assert isinstance(Point(Quantity.ACV, 1.0, 1000.0), PointLike)


# ------------------------------------------------------------------ Protocols

class _Source:
    model_id = "x"

    def validate(self, point):
        return []

    def apply(self, point):
        return SourceState(actual_value=1.0, unit="V")

    def standby(self):
        pass

    def health(self):
        return SourceHealth()


class _Meter:
    model_id = "y"

    def supports(self, quantity, connection):
        return True

    def configure(self, point):
        return MeterConfig()

    def read(self, n):
        return Reading.from_samples([1.0] * n)


class TestProtocols:
    def test_a_plain_class_satisfies_source_role(self):
        assert isinstance(_Source(), SourceRole)

    def test_a_plain_class_satisfies_meter_role(self):
        assert isinstance(_Meter(), MeterRole)

    def test_a_meter_is_not_a_source(self):
        # Only the source has a lifecycle; the asymmetry is deliberate.
        assert not isinstance(_Meter(), SourceRole)

    def test_environment_probe_is_one_method(self):
        class Probe:
            def read(self):
                return Environment(ambient_c=23.0)

        assert isinstance(Probe(), EnvironmentProbe)

    def test_roles_stay_narrow(self):
        # Six members is the ceiling agreed for the seam; a role that grows
        # into a kitchen sink stops being portable to the next instrument.
        for protocol in (SourceRole, MeterRole, EnvironmentProbe):
            members = [n for n in _protocol_members(protocol)]
            assert len(members) <= 6, (protocol.__name__, members)

    def test_temperature_capabilities_are_optional_not_members(self):
        """A meter with no sensor must still satisfy the role.

        ``internal_temperature`` (meter) and ``cal_temperature`` (source) are
        read with a guarded ``getattr`` precisely so that hardware without
        them needs no stub -- making either a protocol member would force
        every implementation to claim a capability it may not have.
        """
        assert "internal_temperature" not in _protocol_members(MeterRole)
        assert "cal_temperature" not in _protocol_members(SourceRole)
        assert isinstance(_Meter(), MeterRole)
        assert isinstance(_Source(), SourceRole)


# ----------------------------------------------------------------- invariants

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

    import caldmm.cal.roles as roles

    state = roles.SourceState(actual_value=10.0, unit="V",
                              uncertainty=roles.Uncertainty(5.0, "PPM", 0.95))
    assert abs(state.expanded_uncertainty - 50e-6) < 1e-12

    leaked = sorted(m for m in sys.modules if m.startswith("caldmm.drivers"))
    assert not leaked, "roles pulled in drivers: " + repr(leaked)

    # roles imports quantities and the stdlib.  Nothing else -- not even the
    # spec loader, which would drag yaml in behind it.
    assert "caldmm.cal.specs" not in sys.modules
    assert "caldmm.cal.adapters" not in sys.modules
    print("OK")
    """)


def test_roles_imports_with_no_hardware_libraries():
    """The architectural invariant, pinned in a clean interpreter.

    A subprocess, so the root conftest's ``MagicMock`` stubs are not in the way
    and a real ``sys.meta_path`` blocker can refuse the imports outright.
    """
    completed = subprocess.run([sys.executable, "-c", IMPORT_HYGIENE_SCRIPT],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert "OK" in completed.stdout

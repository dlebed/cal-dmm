"""Tests for caldmm.cal.adapters — the concrete roles.

Offline: meters are driven as ``MagicMock`` drivers, and the 5700A against the
scriptable :class:`tests.drivers.fake_gpib.FakeTransport`, which keeps an
ordered ``log`` of every command written.  The emitted order matters here more
than anywhere else in the subsystem — a regression that reorders ``OUT`` and
``OPER``, or that reads back before the output has settled, sources the wrong
thing and records it as data.
"""

import math
import os
import subprocess
import sys
import textwrap
from unittest.mock import MagicMock, patch

import pytest

import caldmm

from caldmm.cal.adapters import (
    ADAPTERS,
    CURRENT_POSTS,
    AdapterError,
    Fluke5700ASource,
    HP3458AMeter,
    MeterAdapter,
    StaticEnvironment,
    adapter_class,
    func_name,
    is_serial_address,
    open_meter,
    open_source,
    parse_gpib_address,
    parse_serial_address,
    parse_instrument_spec,
    resolve_func,
)
from caldmm.cal.quantities import Connection, Quantity
from caldmm.cal.roles import (
    Environment,
    EnvironmentProbe,
    MeterRole,
    Point,
    SourceRole,
)
from caldmm.cal.specs import load_model

from tests.drivers.fake_gpib import FakeTransport, stateful_isr


GOOD_IDN = "FLUKE,5720A,5248000,1.0+B+*"


def isr_value():
    from caldmm.drivers.fluke5700a import ISR_OPER, ISR_REMOTE, ISR_SETTLED
    return ISR_REMOTE | ISR_SETTLED | ISR_OPER


def isr_standby():
    """The same status with the output off — what ``STBY`` must produce."""
    from caldmm.drivers.fluke5700a import ISR_REMOTE, ISR_SETTLED
    return ISR_REMOTE | ISR_SETTLED


def calibrator(**overrides):
    """An opened 5700A on a scripted transport.  Returns ``(source, transport)``."""
    from caldmm.drivers.fluke5700a import Fluke5700A

    responses = {
        "FAULT?": ["0"] * 12,
        "*IDN?": GOOD_IDN,
        "*OPT?": '"5725A Attached"',
        "ISR?": str(isr_value()),
        "OUT?": "1.000000E+01,V,0.000000E+00",
        "UNCERT?": "5.000000E+00,PPM,365",
        "CAL_CONF?": "CONF95",
        "RANGE?": "DC11V",
        "STATE?": "0,IDLE",
        "ONTIME?": "45",
        "CAL_DAYS? CAL": "120",
        "CAL_DAYS? ZERO": "3",
    }
    responses.update(overrides)
    transport = FakeTransport(responses)
    device = Fluke5700A(transport=transport)
    return Fluke5700ASource(device), transport


def emitted(transport, since: int) -> list[str]:
    return transport.log[since:]


# ------------------------------------------------------------ spec resolution

class TestFuncResolution:
    """Quantity -> the driver's own Func member, through the YAML."""

    def test_resistance_is_four_wire_or_two_wire_by_connection(self):
        spec = load_model("hp3458a")
        assert func_name(spec, Quantity.RES, Connection.FOUR_WIRE) == "OHMF"
        assert func_name(spec, Quantity.RES, Connection.TWO_WIRE) == "OHM"

    def test_resolves_to_the_actual_enum_member(self):
        from caldmm.drivers.hp3458a import Func

        spec = load_model("hp3458a")
        assert resolve_func(spec, Quantity.RES, Connection.FOUR_WIRE) is Func.OHMF
        assert resolve_func(spec, Quantity.DCV) is Func.DCV


# ------------------------------------------------------------------- meters

class TestMeterCapability:


    def test_3458a_supports_both_resistance_topologies(self):
        meter = HP3458AMeter(MagicMock(), model_id="hp3458a")
        assert meter.supports(Quantity.RES, Connection.FOUR_WIRE)
        assert meter.supports(Quantity.RES, Connection.TWO_WIRE)


    def test_supports_tolerates_junk(self):
        meter = HP3458AMeter(MagicMock(), model_id="hp3458a")
        assert not meter.supports("frequency", Connection.TWO_WIRE)


class TestHP3458AConfigure:
    def test_ac_technique_and_band_precede_the_function(self):
        # SETACV and ACBAND configure the ac path; sent after the function
        # they re-trigger configuration and the reading that comes back is not
        # the one the accuracy column describes.
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.ACV, 1.0, 1000.0))
        order = [call[0] for call in device.mock_calls]
        assert order.index("set_acv_method") < order.index("set_func")
        assert order.index("set_acband") < order.index("set_func")

    def test_ac_band_defaults_to_the_tone(self):
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(Point(Quantity.ACV, 1.0, 1000.0))
        device.set_acband.assert_called_once_with(1000.0, 1000.0)
        assert config.applied["ac_band"] == (1000.0, 1000.0)

    def test_mode_supplies_the_technique(self):
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(Point(Quantity.ACV, 1.0, 1000.0, mode="ana"))
        device.set_acv_method.assert_called_once_with("ANA")
        assert config.mode == "ana"

    def test_four_wire_resistance_selects_ohmf(self):
        from caldmm.drivers.hp3458a import Func

        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(
            Point(Quantity.RES, 10e3, connection=Connection.FOUR_WIRE))
        device.set_func.assert_called_once_with(Func.OHMF, 10e3)
        assert config.func == "OHMF"

    def test_azero_and_ocomp_go_through_the_driver_setters(self):
        # L11: the driver grew set_autozero()/set_ocomp() precisely so this
        # adapter would stop sending raw AZERO/OCOMP strings and leaving
        # HP3458A.autozero / .ocomp reporting the constructor's values.
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.RES, 10e3,
                              connection=Connection.FOUR_WIRE))
        device.set_autozero.assert_called_once_with(True)
        device.set_ocomp.assert_called_once_with(True)
        sent = [call.args[0] for call in device.cmd.call_args_list]
        assert not any(cmd.startswith(("AZERO", "OCOMP")) for cmd in sent)

    def test_a_driver_without_the_setters_still_gets_the_commands(self):
        # The fallback keeps an older driver working rather than failing at
        # the one knob the ohms specification depends on.
        class _Old:
            name = "3458a"

            def __init__(self):
                self.sent = []

            def cmd(self, text):
                self.sent.append(text)

            def set_func(self, func, max_input):
                pass

            def set_acband(self, low, high):
                pass

            nplc = 0.0

        device = _Old()
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.RES, 10e3,
                              connection=Connection.FOUR_WIRE))
        assert "AZERO ON" in device.sent and "OCOMP ON" in device.sent

    def test_ocomp_is_not_claimed_on_a_range_that_cannot_do_it(self):
        # L88: "The multimeter can only perform offset compensation on the
        # 10 Ohm through 100 kOhm ranges" (UG p.96, 3458a-user-guide.txt
        # 3345-3347).  The model file asks for it on the whole function.
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(Point(Quantity.RES, 1e6,
                                       connection=Connection.FOUR_WIRE))
        device.set_ocomp.assert_called_once_with(False)
        assert config.applied["ocomp"] is False
        assert "100 k" in config.applied["ocomp_note"]

    def test_ocomp_goes_off_outside_resistance(self):
        # Measured: the driver connects with OCOMP ON and a DCI
        # burst then never reached MEM ("MCOUNT? kept returning sample
        # readings").  Every non-resistance point turns it off.
        for quantity, value in ((Quantity.DCI, 1e-3), (Quantity.DCV, 1.0)):
            device = MagicMock()
            meter = HP3458AMeter(device, model_id="hp3458a")
            meter.configure(Point(quantity, value))
            device.set_ocomp.assert_called_once_with(False)

    def test_ocomp_is_applied_where_the_meter_implements_it(self):
        for value in (10.0, 100e3):
            device = MagicMock()
            meter = HP3458AMeter(device, model_id="hp3458a")
            config = meter.configure(Point(Quantity.RES, value,
                                           connection=Connection.FOUR_WIRE))
            device.set_ocomp.assert_called_once_with(True)
            assert config.applied["ocomp"] is True
            assert "ocomp_note" not in config.applied

    def test_resistance_programs_a_range_scaled_settling_delay(self):
        # The RES ranges carry `apply: {delay_s: ...}` so the meter waits out
        # the test leads' dielectric absorption before the first sample.
        # Without it the adapter left DELAY at whatever the last point set.
        for value, delay in ((10.0, 0.1), (10e3, 0.25), (10e6, 1.0)):
            device = MagicMock()
            meter = HP3458AMeter(device, model_id="hp3458a")
            config = meter.configure(
                Point(Quantity.RES, value, connection=Connection.FOUR_WIRE))
            assert device.delay == pytest.approx(delay)
            assert config.applied["delay_s"] == pytest.approx(delay)

    def test_the_resistance_delay_rises_with_the_range_and_stops_at_two_seconds(self):
        # "the higher the range - the longer the delay.  Don't go with too
        # long delay though" -- monotonic, and capped.
        #
        # The cap was the brief's 1 s until a bench measurement superseded
        # it: the 100 MOhm point taken at 2 s and at 8 s read +224.838
        # and +232.856 ppm, so 4x the settling moved it 3.6 % of the deviation
        # and in the wrong direction for a still-charging cable.  The brief was an
        # estimate made before any measurement; that is a measurement, so the
        # ceiling is 2.0 s.  It stays a ceiling, and a hard one -- the brief's
        # actual point, that a long delay costs the run, is untouched by which
        # number it is.
        spec = load_model("hp3458a")
        ranges = spec.function(Quantity.RES).ranges
        delays = [r.apply["delay_s"] for r in ranges]
        assert len(delays) == len(ranges)          # every range states one
        assert delays == sorted(delays)
        assert min(delays) >= 0.1
        assert max(delays) == 2.0
        # 100 MOhm and 1 GOhm are both computed from 10 MOhm in parallel with
        # the input (p.17 footnote 4) and are driven identically -- 500 nA,
        # 5.0 V test, 5 V open circuit (3458a-specifications.md:172-173) -- so
        # neither can want less settling than the other.  That 2026-08-24 edit
        # raised only the range it had measured and left 1 GOhm a second short.
        by_fs = {r.fs: r.apply["delay_s"] for r in ranges}
        assert by_fs[1e9] == pytest.approx(by_fs[1e8])

    def test_the_resistance_delay_does_not_shorten_the_settling_time(self):
        # _settle_seconds takes the maximum of the function's 3 s and the
        # applied delay; a 1 s delay must not become a 1 s settle.
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(
            Point(Quantity.RES, 10e6, connection=Connection.FOUR_WIRE))
        assert config.settle_s == pytest.approx(3.0)

    def test_a_point_can_still_override_the_range_delay(self):
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(
            Point(Quantity.RES, 10.0, connection=Connection.FOUR_WIRE,
                  apply={"delay_s": 0.5}))
        assert device.delay == pytest.approx(0.5)
        assert config.applied["delay_s"] == pytest.approx(0.5)

    def test_dc_points_are_not_given_a_resistance_delay(self):
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(Point(Quantity.DCV, 10.0))
        assert "delay_s" not in config.applied

    def test_dc_volts_applies_nplc_and_records_it(self):
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(Point(Quantity.DCV, 10.0))
        assert device.nplc == 100
        assert config.nplc == 100
        assert config.range_fs == 10.0
        assert config.resolution == pytest.approx(1e-7)

    def test_settle_comes_from_the_spec(self):
        meter = HP3458AMeter(MagicMock(), model_id="hp3458a")
        assert meter.configure(Point(Quantity.DCV, 10.0)).settle_s == 2.0

    def test_point_can_override_settle(self):
        meter = HP3458AMeter(MagicMock(), model_id="hp3458a")
        config = meter.configure(Point(Quantity.DCV, 10.0, settle_s=30.0))
        assert config.settle_s == 30.0

    def test_no_range_holds_the_value(self):
        meter = HP3458AMeter(MagicMock(), model_id="hp3458a")
        with pytest.raises(AdapterError, match="no DCV range"):
            meter.configure(Point(Quantity.DCV, 5000.0))

    def test_unknown_mode_is_refused(self):
        meter = HP3458AMeter(MagicMock(), model_id="hp3458a")
        with pytest.raises(AdapterError, match="no mode"):
            meter.configure(Point(Quantity.ACV, 1.0, 1000.0, mode="turbo"))

    def test_an_ac_point_with_no_frequency_is_refused(self):
        # Not silently skipped: ACBAND would keep the previous point's band,
        # which on the 3458A selects both the settling and the accuracy table.
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        with pytest.raises(AdapterError, match="needs a frequency"):
            meter.configure(Point(Quantity.ACV, 1.0, 0.0))
        device.set_acband.assert_not_called()

    def test_a_bad_explicit_band_is_refused(self):
        meter = HP3458AMeter(MagicMock(), model_id="hp3458a")
        with pytest.raises(AdapterError, match="ac_band"):
            meter.configure(Point(Quantity.ACV, 1.0, 1000.0,
                                  apply={"ac_band": [0.0, 2000.0]}))
        with pytest.raises(AdapterError, match="ac_band"):
            meter.configure(Point(Quantity.ACV, 1.0, 1000.0,
                                  apply={"ac_band": [2000.0, 100.0]}))

    def test_a_frequency_on_a_dc_point_is_refused(self):
        # The same plan typo Fluke5700ASource.validate() reports, caught
        # meter-side so it does not need a calibrator in the loop.
        meter = HP3458AMeter(MagicMock(), model_id="hp3458a")
        with pytest.raises(AdapterError, match="dc quantity"):
            meter.configure(Point(Quantity.DCV, 10.0, 1000.0))


# ------------------------------------------------ 3458A sampled-ac technique

class TestHP3458ASyncTechnique:
    """H01, M05 and M60: what a SYNC point is actually configured with.

    The three settings the SYNC accuracy table assumes and nothing was
    sending: the reading resolution (datasheet p.26 adds "(Resolution in %)
    x 1" of reading, so the *default* 0.01 % is an unbudgeted 100 ppm), the
    level filter (datasheet p.25 note 3, "LFILTER ON recommended" for every
    SYNC column to 50 kHz) and the sync-source fallback (UG p.99: a lost
    signal silently completes the reading in *random* sampling).
    """

    def test_the_resolution_is_sent_and_recorded(self):
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(Point(Quantity.ACV, 1.0, 1000.0))
        sent = [call.args[0] for call in device.cmd.call_args_list]
        assert "RES 0.001" in sent
        assert config.applied["resolution_pct"] == pytest.approx(0.001)
        assert config.applied["resolution_digits"] == pytest.approx(7.5)

    def test_the_resolution_follows_the_function_command(self):
        # A function command with its %_resolution parameter omitted takes
        # the *default* (UG RANGE entry, p.327: 0.01 % for SETACV SYNC), so a
        # RES sent before ACV would be thrown away by the ACV.
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.ACV, 1.0, 1000.0))
        order = [call[0] for call in device.mock_calls]
        cmds = [call.args[0] for call in device.cmd.call_args_list]
        assert "RES 0.001" in cmds
        # set_func is the ACV; the RES goes out after it, through cmd().
        res_call = [i for i, call in enumerate(device.mock_calls)
                    if call[0] == "cmd" and call.args
                    and call.args[0] == "RES 0.001"][0]
        assert order.index("set_func") < res_call

    def test_a_dc_point_never_sends_a_resolution(self):
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(Point(Quantity.DCV, 10.0))
        sent = [call.args[0] for call in device.cmd.call_args_list]
        assert not any(cmd.startswith("RES") for cmd in sent)
        assert "resolution_pct" not in config.applied

    def test_random_sampling_is_not_given_a_resolution(self):
        # UG p.330: RNDM "has a fixed resolution of 4.5 digits that cannot be
        # changed", so a RES here would claim something untrue.
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(Point(Quantity.ACV, 1.0, 1000.0, mode="rndm"))
        sent = [call.args[0] for call in device.cmd.call_args_list]
        assert not any(cmd.startswith("RES") for cmd in sent)
        assert config.applied["resolution_digits"] == pytest.approx(4.5)

    @pytest.mark.parametrize("fs,mode,expected", [
        # Datasheet maximum-resolution columns: SYNC p.25, ANA p.28, RNDM p.30.
        (0.01, "sync", 10e-9), (0.1, "sync", 10e-9), (1.0, "sync", 100e-9),
        (10.0, "sync", 1e-6), (100.0, "sync", 10e-6), (1000.0, "sync", 100e-6),
        (0.01, "ana", 10e-9), (0.1, "ana", 100e-9), (1.0, "ana", 1e-6),
        (10.0, "ana", 10e-6), (100.0, "ana", 100e-6), (1000.0, "ana", 1e-3),
        (0.01, "rndm", 1e-6), (0.1, "rndm", 10e-6), (1.0, "rndm", 100e-6),
        (10.0, "rndm", 1e-3), (100.0, "rndm", 10e-3), (1000.0, "rndm", 100e-3),
    ])
    def test_the_reading_resolution_is_the_datasheet_figure(self, fs, mode,
                                                            expected):
        # The per-range `resolution` in the model file is the SYNC maximum;
        # reading a point in ANA or RNDM at that LSD understates the
        # quantisation term by one to three decades.
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(
            Point(Quantity.ACV, fs * 0.9, 1000.0, mode=mode))
        assert config.range_fs == pytest.approx(fs)
        assert config.resolution == pytest.approx(expected)

    def test_a_coarser_sync_resolution_gives_a_coarser_lsd(self):
        # UG p.330: for SETACV SYNC, 0.001 = 7.5 digits, 0.01 = 6.5.  The
        # default the meter used before this fix was 0.01 -- a 1 uV LSD on
        # the 1 V range, not the 100 nV the model file records.
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.sync_resolution_pct = 0.01
        config = meter.configure(Point(Quantity.ACV, 1.0, 1000.0))
        sent = [call.args[0] for call in device.cmd.call_args_list]
        assert "RES 0.01" in sent
        assert config.applied["resolution_digits"] == pytest.approx(6.5)
        assert config.resolution == pytest.approx(1e-6)

    def test_an_unknown_sync_resolution_is_refused(self):
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.sync_resolution_pct = 0.003
        with pytest.raises(AdapterError, match="resolution"):
            meter.configure(Point(Quantity.ACV, 1.0, 1000.0))

    def test_sync_points_hold_the_sync_source_and_filter(self):
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(Point(Quantity.ACV, 1.0, 1000.0))
        device.set_sync_source.assert_called_once_with("LEVEL", "HOLD")
        device.set_lfilter.assert_called_once_with(True)
        assert config.applied["sync_source"] == "LEVEL,HOLD"
        assert config.applied["lfilter"] is True

    def test_the_filter_comes_off_above_the_datasheet_band(self):
        # Note 3 covers the SYNC columns to 50 kHz; the filter's own 3 dB
        # point is 75 kHz (UG p.283), so leaving it on above that band
        # attenuates the very signal the level trigger is looking at.
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(Point(Quantity.ACV, 1.0, 100e3, mode="sync"))
        device.set_lfilter.assert_called_once_with(False)
        assert config.applied["lfilter"] is False

    def test_the_sync_controls_precede_the_function(self):
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.ACV, 1.0, 1000.0))
        order = [call[0] for call in device.mock_calls]
        assert order.index("set_lfilter") < order.index("set_func")
        assert order.index("set_sync_source") < order.index("set_func")

    def test_other_techniques_leave_the_sync_controls_alone(self):
        # SSRC's mode parameter "applies only to synchronous ACV or ACDCV"
        # (UG p.353) and LFILTER is on the level-trigger path, which ANA and
        # RNDM do not use: touching either would record a knob that did
        # nothing.
        for mode in ("ana", "rndm"):
            device = MagicMock()
            meter = HP3458AMeter(device, model_id="hp3458a")
            config = meter.configure(
                Point(Quantity.ACV, 1.0, 1000.0, mode=mode))
            device.set_sync_source.assert_not_called()
            device.set_lfilter.assert_not_called()
            assert "sync_source" not in config.applied
            assert "lfilter" not in config.applied

    def test_a_driver_without_the_sync_setters_is_refused(self):
        # Silently skipping them would leave the row claiming SYNC's table
        # while the meter could still fall back to random sampling.
        class _Old:
            name = "3458a"

            def cmd(self, text):
                pass

            def set_func(self, func, max_input):
                pass

            def set_acv_method(self, method):
                pass

            def set_acband(self, low, high):
                pass

            nplc = 0.0

        meter = HP3458AMeter(_Old(), model_id="hp3458a")
        with pytest.raises(AdapterError, match="SSRC|LFILTER"):
            meter.configure(Point(Quantity.ACV, 1.0, 1000.0))


class TestHP3458ANullAndRangeHold:
    """M03: the dc figures assume MATH NULL on a range held over 4 minutes."""

    def test_the_record_says_math_null_was_not_used(self):
        # Datasheet p.12 footnote 5: without MATH NULL add 0.15 ppm of range
        # on 10 V, 0.7 ppm on 0.1 V; under 4 minutes on a range, 0.25 / 1.7 /
        # 17 ppm.  Nothing here sends MATH NULL, so the row has to say so.
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(Point(Quantity.DCV, 10.0))
        assert config.applied["math_null"] is False
        assert config.applied["range_held_s"] == pytest.approx(0.0)

    def test_holding_a_range_accumulates_its_hold_time(self, monkeypatch):
        import caldmm.cal.adapters as adapters

        now = [1000.0]
        monkeypatch.setattr(adapters, "_now", lambda: now[0])
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.DCV, 10.0))
        now[0] += 300.0
        config = meter.configure(Point(Quantity.DCV, 9.0))
        assert config.applied["range_held_s"] == pytest.approx(300.0)

    def test_changing_the_range_restarts_the_clock(self, monkeypatch):
        import caldmm.cal.adapters as adapters

        now = [1000.0]
        monkeypatch.setattr(adapters, "_now", lambda: now[0])
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.DCV, 10.0))
        now[0] += 300.0
        config = meter.configure(Point(Quantity.DCV, 1.0))     # 1 V range
        assert config.applied["range_held_s"] == pytest.approx(0.0)


class TestScpiErrorQueue:
    """L02: the ``applied`` column may not claim a knob the meter rejected."""

    def _device(self, errors):
        device = MagicMock()
        device.error_queue.return_value = list(errors)
        return device


    def test_the_3458a_is_not_asked_for_a_scpi_error_queue(self):
        # It has none: its own error register is AUXERR?/ERRSTR?, and sending
        # SYST:ERR? to a 3458A is a syntax error.
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.DCV, 10.0))
        device.error_queue.assert_not_called()


#: (adapter class, model id, a point, the range it must be configured on).
MANUAL_RANGE_CASES = [
    (HP3458AMeter, "hp3458a", Point(Quantity.DCV, 10.0), 10.0),
]


class TestManualRanging:
    """Global Constraint §9: every point is read on a **fixed** range.

    Autoranging picks the range from the signal, which is the one thing a
    verification must not do: the range decides the ppm-of-range term, the
    resolution, the input impedance and (on ohms) the test current, and a
    reading taken on a range nobody recorded cannot be judged at all.  The
    34401A service guide says as much for its own verification (p.31: "It is
    also assumed that manual ranging is enabled to ensure proper full scale
    range selection").
    """

    @pytest.mark.parametrize("cls,model_id,point,fs", MANUAL_RANGE_CASES)
    def test_every_adapter_declares_the_capability(self, cls, model_id,
                                                   point, fs):
        assert cls(MagicMock(), model_id=model_id).supports_manual_range is True

    @pytest.mark.parametrize("cls,model_id,point,fs", MANUAL_RANGE_CASES)
    def test_the_range_is_fixed_and_recorded(self, cls, model_id, point, fs):
        device = MagicMock()
        config = cls(device, model_id=model_id).configure(point)
        assert config.range_fs == pytest.approx(fs)
        assert config.range_arg == pytest.approx(fs)
        assert config.applied["autorange"] is False
        assert config.applied["range"] == pytest.approx(fs)
        # Whatever the driver spells it, the range argument that went out is
        # a real range, never the -1/AUTO sentinel.
        assert device.set_func.call_args[0][1] == pytest.approx(fs)

    @pytest.mark.parametrize("cls,model_id,point,fs", MANUAL_RANGE_CASES)
    def test_no_adapter_ever_enables_autorange(self, cls, model_id, point, fs):
        device = MagicMock()
        cls(device, model_id=model_id).configure(point)
        sent = [str(call.args[0]) for call in device.cmd.call_args_list
                if call.args]
        assert not any("AUTO ON" in cmd or cmd.endswith("AUTO")
                       for cmd in sent), sent
        assert not any("ARANGE ON" in cmd for cmd in sent), sent

    def test_an_ac_point_fixes_its_range_too(self):
        # RANGE applies per function on the 3458A, and a SYNC point is no
        # exception: the ac accuracy table is "% of reading + % of range".
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(Point(Quantity.ACV, 1.0, 1000.0))
        sent = [call.args[0] for call in device.cmd.call_args_list]
        assert "ARANGE OFF" in sent
        assert device.set_func.call_args[0][1] == pytest.approx(1.0)
        assert config.applied["autorange"] is False

    def test_3458a_disables_autorange_before_selecting_the_range(self):
        # UG p.240: "ARANGE [control] ... Power-on control = ON", so a meter
        # that has just been switched on is autoranging until told otherwise.
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.DCV, 10.0))
        order = [call[0] for call in device.mock_calls]
        arange = [i for i, call in enumerate(device.mock_calls)
                  if call[0] == "cmd" and call.args
                  and call.args[0] == "ARANGE OFF"]
        assert arange, device.mock_calls
        assert arange[0] < order.index("set_func")

    def test_3458a_turns_math_off_so_the_recorded_regime_is_true(self):
        """``_range_hold`` records ``math_null: False`` on every dc row.

        That is the datasheet's p.12 footnote 5 regime -- without MATH NULL,
        add 0.15 ppm of range on 10 V and 0.7 ppm on 0.1 V -- and until this
        command was sent the record was an assumption about a state nobody
        had asserted.  A NULL left registered by the front panel or an
        earlier session would have made the row's own claim false.
        """
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        config = meter.configure(Point(Quantity.DCV, 10.0))
        sent = [call.args[0] for call in device.cmd.call_args_list
                if call.args]
        assert "MATH OFF" in sent
        assert config.applied["math_null"] is False
        order = [call[0] for call in device.mock_calls]
        math_off = [i for i, call in enumerate(device.mock_calls)
                    if call[0] == "cmd" and call.args
                    and call.args[0] == "MATH OFF"]
        assert math_off[0] < order.index("set_func")


    def test_selecting_no_range_at_all_is_refused(self):
        meter = HP3458AMeter(MagicMock(), model_id="hp3458a")
        with pytest.raises(AdapterError, match="[Aa]utorange"):
            meter._set_function("DCV", None)


class TestRangeReadback:
    """The engine verifies the configured range before energizing."""

    def test_3458a_reads_range_back(self):
        device = MagicMock()
        device.query.return_value = "1.20000000E+01"    # 10 V range, fs 12 V
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.DCV, 10.0))
        assert meter.range_readback() == pytest.approx(10.0)
        device.query.assert_called_once_with("RANGE?")

    def test_3458a_reports_the_range_it_is_really_on(self):
        device = MagicMock()
        device.query.return_value = "1.20000000E+02"    # 100 V range
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.DCV, 10.0))
        assert meter.range_readback() == pytest.approx(100.0)


    def test_a_readback_before_configure_is_refused(self):
        meter = HP3458AMeter(MagicMock(), model_id="hp3458a")
        with pytest.raises(AdapterError, match="before configure"):
            meter.range_readback()

    def test_an_unreadable_answer_is_an_error(self):
        device = MagicMock()
        device.query.return_value = "OVLD"
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.DCV, 10.0))
        with pytest.raises(AdapterError, match="RANGE"):
            meter.range_readback()


#: A 3458A resistance function with the 20 % overrange the manual's own range
#: table implies ("For OHM or OHMF: >1.2E3 to 1.2E4 selects 10 kOhm, full
#: scale 12 kOhm", UG p.276).  The shipped file grows these in Task 3; this
#: fixture is what pins the *adapter's* half of M01/M04 either way.
OVERRANGE_YAML = """
schema_version: 1
model_id: hp3458a
display_name: "3458A (overrange fixture)"
role: meter
interval_basis: since_cal
intervals: [1y]
default_interval: 1y
overload_sentinel: 9.9e+37
driver: {module: caldmm.drivers.hp3458a, cls: HP3458A, func_enum: Func}
adapter: "caldmm.cal.adapters:HP3458AMeter"
functions:
  - quantity: RES
    connections:
      4w: {func: OHMF}
      2w: {func: OHM}
    ranges:
      - fs: 1000.0
        range_arg: 1000.0
        resolution: 1.0e-4
        overrange: 1.2
        limits: {1y: {ppm_reading: 10.0, ppm_range: 1.0}}
      - fs: 10000.0
        range_arg: 10000.0
        resolution: 1.0e-3
        overrange: 1.2
        limits: {1y: {ppm_reading: 10.0, ppm_range: 0.5}}
      - fs: 100000.0
        range_arg: 100000.0
        resolution: 1.0e-2
        overrange: 1.2
        limits: {1y: {ppm_reading: 15.0, ppm_range: 0.5}}
"""

#: A generic dc-volts meter that declares a 20 % overrange on every range.
OVERRANGE_GENERIC_YAML = """
schema_version: 1
model_id: example_dmm
display_name: "Example DMM (overrange fixture)"
role: meter
interval_basis: since_cal
intervals: [1y]
default_interval: 1y
overload_sentinel: 9.9e+37
driver: {module: caldmm.drivers.example_dmm, cls: ExampleDMM, func_enum: Func}
adapter: "caldmm.cal.adapters:ExampleDMMMeter"
functions:
  - quantity: DCV
    func: DCV
    connections: [2w]
    ranges:
      - fs: 10.0
        range_arg: 10.0
        resolution: 1.0e-5
        overrange: 1.2
        input_impedance_ohm: 1.0e+10
        limits: {1y: {ppm_reading: 35.0, ppm_range: 5.0}}
      - fs: 100.0
        range_arg: 100.0
        resolution: 1.0e-4
        overrange: 1.2
        input_impedance_ohm: 1.0e+7
        limits: {1y: {ppm_reading: 45.0, ppm_range: 6.0}}
      - fs: 1000.0
        range_arg: 1000.0
        resolution: 1.0e-3
        input_impedance_ohm: 1.0e+7
        note: "no 20 % overrange on this range (p.216 footnote [3])"
        limits: {1y: {ppm_reading: 45.0, ppm_range: 6.0}}
"""

class TestOverrangeSelection:
    """M01 / M04: a meter is *configured* with its overrange, judged without.

    The 5700A's 100 % points sit at 11 V, 110 V and 1.1 V, and its
    characterized resistances land a few hundred ppm either side of the
    nominal.  Refusing the overrange puts every one of them on the next range
    up -- a decade coarser LSD, a ppm-of-range term evaluated on a ten times
    larger range, a different ohms test current -- which is honest but
    useless.
    """

    def test_a_characterized_resistance_stays_on_its_own_range(self):
        from caldmm.cal.specs import parse_model_text

        spec = parse_model_text(OVERRANGE_YAML, "<overrange>")
        device = MagicMock()
        meter = HP3458AMeter(device, spec=spec, model_id="hp3458a")
        config = meter.configure(Point(Quantity.RES, 10e3 * (1 + 300e-6),
                                       connection=Connection.FOUR_WIRE))
        assert config.range_fs == pytest.approx(10e3)
        assert config.range_arg == pytest.approx(10e3)
        assert config.resolution == pytest.approx(1e-3)


    def test_the_overrange_never_reaches_the_limit_evaluation(self):
        # The range term is written against the nominal full scale: 11 V is
        # *configured* on the 10 V range and *judged* on the 100 V row.
        from caldmm.cal.specs import parse_model_text, evaluate_limit

        spec = parse_model_text(OVERRANGE_GENERIC_YAML, "<overrange-generic>")
        result = evaluate_limit(spec, Quantity.DCV, 11.0, interval="1y")
        assert result.range_spec.fs == pytest.approx(100.0)


class TestInternalTemperature:
    """The optional capability: real where the hardware has a sensor, an
    honest ``None`` everywhere else."""

    class _Sensor:
        name = "dmm"

        def has_internal_temp(self):
            return True

        def get_temp(self):
            return 38.75

    class _NoSensor:
        name = "dmm"

        def has_internal_temp(self):
            return False

    def test_3458a_reads_temp_through_the_driver(self):
        meter = HP3458AMeter(self._Sensor(), model_id="hp3458a")
        assert meter.internal_temperature() == 38.75

    def test_gated_on_the_capability_hook(self):
        meter = HP3458AMeter(self._NoSensor(), model_id="hp3458a")
        assert meter.internal_temperature() is None

    def test_a_driver_without_the_hook_is_none(self):
        class _Bare:
            name = "dmm"

        meter = HP3458AMeter(_Bare(), model_id="hp3458a")
        assert meter.internal_temperature() is None


# ------------------------------------------------------------------ reading

class TestOverloadSentinels:
    """The sentinel is per model, and it is counted, not averaged."""

    def _meter(self, cls, model_id, samples):
        device = MagicMock()
        device.read_samples.return_value = list(samples)
        meter = cls(device, model_id=model_id)
        meter.configure(Point(Quantity.DCV, 10.0))
        return meter


    def test_3458a_does_not_treat_1e35_as_an_overload(self):
        # Same number, different meter: 1e35 is an ordinary (huge) reading on
        # a 3458A, whose sentinel is 9.9e37.
        meter = self._meter(HP3458AMeter, "hp3458a", [10.0, 1e35, 10.2])
        reading = meter.read(3)
        assert reading.overloads == 0
        assert reading.n == 3
        assert reading.mean == pytest.approx(1e35 / 3, rel=1e-9)

    def test_3458a_excludes_its_own_sentinel(self):
        meter = self._meter(HP3458AMeter, "hp3458a", [10.0, 9.9e37, 10.2])
        reading = meter.read(3)
        assert reading.overloads == 1
        assert reading.mean == pytest.approx(10.1)

    def test_reading_carries_the_range_resolution(self):
        meter = self._meter(HP3458AMeter, "hp3458a", [10.0, 10.0])
        reading = meter.read(2)
        assert reading.resolution == pytest.approx(1e-7)
        assert reading.unit == "V"

    def test_discarded_samples_come_off_the_front(self):
        device = MagicMock()
        device.read_samples.return_value = [99.0, 10.0, 10.2]
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.DCV, 10.0))
        reading = meter.read(2, discard=1)
        device.read_samples.assert_called_once_with(3)
        assert reading.discarded == 1
        assert reading.mean == pytest.approx(10.1)

    def test_zero_samples_is_refused(self):
        device = MagicMock()
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.DCV, 10.0))
        with pytest.raises(ValueError):
            meter.read(0)

    def test_read_before_configure_is_refused(self):
        # A None resolution would quietly drop the quantisation component out
        # of the budget, shrinking U and the guard band and making a PASS more
        # likely -- the one direction this subsystem must never fail in.
        device = MagicMock()
        device.read_samples.return_value = [10.0, 10.0]
        meter = HP3458AMeter(device, model_id="hp3458a")
        with pytest.raises(AdapterError, match="before configure"):
            meter.read(2)
        device.read_samples.assert_not_called()

    def test_a_failed_configure_does_not_leave_a_stale_range(self):
        device = MagicMock()
        device.read_samples.return_value = [10.0, 10.0]
        meter = HP3458AMeter(device, model_id="hp3458a")
        meter.configure(Point(Quantity.DCV, 10.0))
        with pytest.raises(AdapterError):
            meter.configure(Point(Quantity.DCV, 5000.0))     # no range holds it
        with pytest.raises(AdapterError, match="before configure"):
            meter.read(2)


# ------------------------------------------------------------- 5700A source

class TestFluke5700ASource:
    def test_satisfies_the_source_role(self):
        source, _ = calibrator()
        assert isinstance(source, SourceRole)
        assert source.model_id == "fluke5700a"

    def test_apply_emits_the_documented_order(self):
        source, transport = calibrator()
        mark = len(transport.log)
        source.apply(Point(Quantity.DCV, 10.0))
        assert emitted(transport, mark) == [
            "STBY",                                  # safe transition
            "ISR?",                                  # is the range locked?
            "OUT 1.0000000000E+01 V, 0.000000E+00 HZ",
            "FAULT?",                                # drain after OUT
            "EXTSENSE OFF",                          # 2w function: asserted,
            "RCOMP OFF",                             # not merely left alone
            "FAULT?",                                # drain before OPER
            "OPER",
            "ISR?",                                  # settle poll
            "ISR?",                                  # OPER verified after settling
            "ISR?",                                  # status snapshot
            "OUT?",                                  # the reference value
            "UNCERT?",                               # the reference uncertainty
            "CAL_CONF?",                             # at which confidence
            "RANGE?",
        ]

    def test_apply_reads_back_rather_than_assuming(self):
        source, transport = calibrator(
            **{"OUT?": "9.999950E+00,V,0.000000E+00"})
        state = source.apply(Point(Quantity.DCV, 10.0))
        assert state.actual_value == pytest.approx(9.99995)
        assert state.unit == "V"
        assert state.range_id == "DC11V"
        assert state.settled and state.operate
        assert state.raw["OPER"] is True

    def test_resistance_reference_is_the_characterized_readback(self):
        # OUT? in ohms returns what the calibrator really sources, up to
        # 500 ppm from the nominal.  Using the request instead folds the
        # calibrator's ohms characterization into the UUT's error.
        source, transport = calibrator(**{
            "OUT?": "1.900384E+04,OHM,0.000000E+00",
            "RANGE?": "RES19K",
        })
        state = source.apply(Point(Quantity.RES, 19e3))
        assert state.actual_value == pytest.approx(19003.84)
        assert state.actual_value != 19e3

    def test_ohms_never_carries_a_frequency(self):
        source, transport = calibrator(**{
            "OUT?": "1.900384E+04,OHM,0.000000E+00"})
        mark = len(transport.log)
        source.apply(Point(Quantity.RES, 19e3))
        assert "OUT 1.9000000000E+04 OHM" in emitted(transport, mark)
        assert not any("HZ" in cmd for cmd in emitted(transport, mark))

    def test_wideband_point_engages_wband_from_standby(self):
        source, transport = calibrator(**{
            "OUT?": "1.000000E+00,V,1.000000E+06",
            "RANGE?": "WB1_1V"})
        # The function change goes through standby(), which now verifies that
        # the output really came off, so the status has to follow OPER/STBY.
        transport.script("ISR?", stateful_isr(transport))
        mark = len(transport.log)
        source.apply(Point(Quantity.WBV, 1.0, frequency_hz=1e6))
        sent = emitted(transport, mark)
        assert "WBAND ON" in sent
        assert sent.index("STBY") < sent.index("WBAND ON")
        assert sent.index("WBAND ON") < sent.index(
            "OUT 1.0000000000E+00 V, 1.000000E+06 HZ")

    def test_standard_point_after_wideband_disengages_it(self):
        source, transport = calibrator(**{
            "OUT?": ["1.000000E+00,V,1.000000E+06",
                     "1.000000E+01,V,0.000000E+00"],
            "RANGE?": ["WB1_1V", "DC11V"]})
        transport.script("ISR?", stateful_isr(transport))
        source.apply(Point(Quantity.WBV, 1.0, frequency_hz=1e6))
        mark = len(transport.log)
        source.apply(Point(Quantity.DCV, 10.0))
        sent = emitted(transport, mark)
        assert "WBAND OFF" in sent
        assert sent.index("WBAND OFF") < sent.index(
            "OUT 1.0000000000E+01 V, 0.000000E+00 HZ")

    def test_a_standard_point_does_not_touch_wband(self):
        source, transport = calibrator()
        mark = len(transport.log)
        source.apply(Point(Quantity.DCV, 10.0))
        assert not any("WBAND" in cmd for cmd in emitted(transport, mark))

    def test_wbv_points_are_validated_against_the_wideband_envelope(self):
        source, _ = calibrator()
        problems = source.validate(Point(Quantity.WBV, 5.0,
                                         frequency_hz=1e6))
        assert any("3.5 V wideband maximum" in p for p in problems)
        assert source.validate(Point(Quantity.WBV, 1.0,
                                     frequency_hz=1e6)) == []
        # The same 5 V at 1 MHz is legal on the standard ac output, so the
        # envelope really is selected by the quantity.
        assert source.validate(Point(Quantity.ACV, 5.0,
                                     frequency_hz=1e6)) == []

    def test_four_wire_selects_external_sense(self):
        source, transport = calibrator(**{
            "OUT?": "1.900384E+04,OHM,0.000000E+00"})
        mark = len(transport.log)
        source.apply(Point(Quantity.RES, 19e3,
                           connection=Connection.FOUR_WIRE))
        sent = emitted(transport, mark)
        assert "EXTSENSE ON" in sent
        assert sent.index("EXTSENSE ON") < sent.index("OPER")

    def test_leaving_ohms_clears_external_sense(self):
        # EXTSENSE left on from an ohms point trips the *next* point to
        # standby (guide 11.7) and operate() then raises "output did not come
        # on" with nothing pointing at the modifier the adapter left set.
        source, transport = calibrator(**{
            "OUT?": ["1.900384E+04,OHM,0.000000E+00",
                     "1.000000E+01,V,0.000000E+00"],
            "RANGE?": ["RES19K", "DC11V"]})
        source.apply(Point(Quantity.RES, 19e3,
                           connection=Connection.FOUR_WIRE))
        mark = len(transport.log)
        source.apply(Point(Quantity.DCV, 10.0))
        sent = emitted(transport, mark)
        assert "EXTSENSE OFF" in sent
        assert sent.index("EXTSENSE OFF") < sent.index("OPER")

    def test_volts_asserts_both_modifiers_off(self):
        # Also covers the session that inherits a front-panel EXTSENSE ON: the
        # adapter states the configuration it needs rather than assuming it.
        source, transport = calibrator()
        mark = len(transport.log)
        source.apply(Point(Quantity.DCV, 10.0))
        sent = emitted(transport, mark)
        assert "EXTSENSE OFF" in sent and "RCOMP OFF" in sent

    def test_two_wire_rcomp_enables_compensation(self):
        source, transport = calibrator(**{
            "OUT?": "1.900384E+04,OHM,0.000000E+00"})
        mark = len(transport.log)
        source.apply(Point(Quantity.RES, 19e3,
                           connection=Connection.TWO_WIRE_RCOMP))
        assert "RCOMP ON" in emitted(transport, mark)

    def test_uncertainty_is_carried_with_its_confidence(self):
        source, _ = calibrator()
        state = source.apply(Point(Quantity.DCV, 10.0))
        assert state.uncertainty.value == pytest.approx(5.0)
        assert state.uncertainty.unit == "PPM"
        assert state.uncertainty.confidence == 0.95
        assert state.uncertainty.interval_days == 365
        assert state.expanded_uncertainty == pytest.approx(50e-6)

    def test_conf99_is_read_from_the_instrument(self):
        # CAL_CONF is a stored setting that changes what UNCERT? answers for
        # the same output; assuming 95 % understates the reference by a third.
        source, _ = calibrator(**{"CAL_CONF?": "CONF99"})
        state = source.apply(Point(Quantity.DCV, 10.0))
        assert state.uncertainty.confidence == 0.99

    def test_an_unparseable_confidence_is_an_error_not_95_percent(self):
        # Falling back to 95 % would reintroduce exactly the understatement
        # the CAL_CONF? read exists to prevent.
        source, _ = calibrator(**{"CAL_CONF?": "GARBAGE"})
        with pytest.raises(AdapterError, match="CAL_CONF"):
            source.apply(Point(Quantity.DCV, 10.0))

    def test_confidence_is_read_once_per_session(self):
        source, transport = calibrator()
        source.apply(Point(Quantity.DCV, 10.0))
        mark = len(transport.log)
        source.apply(Point(Quantity.DCV, 10.0))
        assert "CAL_CONF?" not in emitted(transport, mark)

    def test_cal_temperature_is_the_cal_activity_answer(self):
        # CAL_TEMP? CAL -- the ambient the stored constants assume, recorded
        # in the run header; the ambient-minus-CAL_TEMP delta drives the
        # calibrator's tempco.
        source, transport = calibrator(**{"CAL_TEMP? CAL": "2.310000E+01"})
        mark = len(transport.log)
        assert source.cal_temperature() == pytest.approx(23.1)
        assert "CAL_TEMP? CAL" in emitted(transport, mark)

    def test_no_specification_is_none_not_zero(self):
        source, transport = calibrator(**{"UNCERT?": "-1.000000E+00,PPM,365"})
        mark = len(transport.log)
        state = source.apply(Point(Quantity.DCV, 10.0))
        assert state.uncertainty is None
        assert state.expanded_uncertainty is None
        assert "CAL_CONF?" not in emitted(transport, mark)

    def test_validate_is_offline_and_lists_everything(self):
        source, transport = calibrator()
        mark = len(transport.log)
        problems = source.validate(Point(Quantity.DCV, 5000.0))
        assert problems
        assert emitted(transport, mark) == []       # no I/O at all

    def test_validate_rejects_wiring_the_model_file_forbids(self):
        # Four-wire sensing exists on every resistance except 100 MOhm, and
        # two-wire compensation only at 19 kOhm and below.  Catching that
        # offline beats faulting at the RCOMP with the output energized.
        source, _ = calibrator()
        assert any("4w" in p for p in source.validate(
            Point(Quantity.RES, 100e6, connection=Connection.FOUR_WIRE)))
        assert any("2w_rcomp" in p for p in source.validate(
            Point(Quantity.RES, 100e3, connection=Connection.TWO_WIRE_RCOMP)))
        assert source.validate(
            Point(Quantity.RES, 19e3, connection=Connection.TWO_WIRE_RCOMP)) == []

    def test_validate_rejects_four_wire_dc_volts(self):
        source, _ = calibrator()
        assert source.validate(
            Point(Quantity.DCV, 10.0, connection=Connection.FOUR_WIRE))

    def test_validate_rejects_a_frequency_on_a_dc_point(self):
        source, _ = calibrator()
        problems = source.validate(Point(Quantity.RES, 19e3, 1000.0))
        assert any("dc quantity" in p for p in problems)

    def test_apply_refuses_an_invalid_point_before_touching_the_bus(self):
        source, transport = calibrator()
        mark = len(transport.log)
        with pytest.raises(AdapterError, match="refusing to source"):
            source.apply(Point(Quantity.DCV, 5000.0))
        assert emitted(transport, mark) == []

    def test_health_reports_the_lifecycle(self):
        source, transport = calibrator(**{
            "STATE?": "18,TRIPPED", "ONTIME?": "12",
            "CAL_DAYS? CAL": "200", "CAL_DAYS? ZERO": "45"})
        mark = len(transport.log)
        health = source.health()
        assert health.tripped is True
        assert health.warm_minutes == 12
        assert health.days_since_cal == 200
        assert health.days_since_zero == 45       # past the 30-day zeros gate
        assert health.operate is True
        sent = emitted(transport, mark)
        assert "ISR?" in sent and "STATE?" in sent and "ONTIME?" in sent
        assert "CAL_DAYS? CAL" in sent and "CAL_DAYS? ZERO" in sent

    def test_health_returns_the_drained_faults(self):
        source, transport = calibrator()
        transport.script("FAULT?", ["3111", "0"] + ["0"] * 8)
        transport.script("EXPLAIN? 3111", "Output Overload")
        health = source.health()
        assert health.faults and health.faults[0][0] == 3111

    def test_standby_verifies_that_the_output_came_off(self):
        # M47: STBY is ignored when the calibrator is not in remote, so the
        # de-energize primitive reads the OPER bit back rather than trusting
        # that the write went out.
        source, transport = calibrator(**{"ISR?": str(isr_standby())})
        mark = len(transport.log)
        source.standby()
        assert emitted(transport, mark) == ["STBY", "ISR?"]

    def test_a_standby_that_did_not_take_effect_raises(self):
        from caldmm.drivers.fluke5700a import Fluke5700AError

        source, transport = calibrator()          # ISR? keeps OPER set
        with pytest.raises(Fluke5700AError, match="OPER"):
            source.standby()


class TestRangeLock:
    """H05: the output really is on the range the plan credits.

    Autoranging selects "the lowest possible range" ([OM 1996] p.4-24), so a
    110 V point pinned to DC1100V is physically on the 220 V range unless the
    range is locked.  Fluke's own verification does it in exactly this order --
    "Set the Calibrator output for 10V, then activate range lock for the 11V
    range.  Now set the Calibrator to 0V OPERATE" ([SM 2007] 3-16, line 4140).
    """

    def _pinned(self):
        return Point(Quantity.DCV, 110.0, range_id="DC1100V")

    def test_apply_locks_the_range_before_sourcing_the_point(self):
        source, transport = calibrator(**{
            "OUT?": "1.100000E+02,V,0.000000E+00", "RANGE?": "DC1100V"})
        mark = len(transport.log)
        source.apply(self._pinned())
        assert emitted(transport, mark) == [
            "STBY",                                  # safe transition
            "ISR?",                                  # is the range locked?
            "OUT 1.1000000000E+03 V, 0.000000E+00 HZ",   # the anchor
            "FAULT?",
            "RANGE?",                                # it really selected it
            "RANGELCK ON",
            "STBY",
            "OUT 1.1000000000E+02 V, 0.000000E+00 HZ",   # the setpoint
            "FAULT?",
            "RANGE?",                                # the lock held
            "EXTSENSE OFF",
            "RCOMP OFF",
            "FAULT?",
            "OPER",                                  # only now is it energized
            "ISR?", "ISR?", "ISR?",
            "OUT?", "UNCERT?", "CAL_CONF?", "RANGE?",
        ]

    def test_a_naturally_ranged_point_never_sends_rangelck(self):
        source, transport = calibrator()
        mark = len(transport.log)
        source.apply(Point(Quantity.DCV, 10.0, range_id="DC11V"))
        assert not any("RANGELCK" in cmd for cmd in emitted(transport, mark))

    def test_an_anchor_that_lands_elsewhere_aborts_before_operate(self):
        source, transport = calibrator(**{"RANGE?": "DC220V"})
        mark = len(transport.log)
        with pytest.raises(AdapterError, match="DC1100V"):
            source.apply(self._pinned())
        sent = emitted(transport, mark)
        assert "OPER" not in sent
        assert "RANGELCK ON" not in sent

    def test_a_lock_that_did_not_hold_aborts_before_operate(self):
        source, transport = calibrator(**{
            "RANGE?": ["DC1100V", "DC220V", "DC220V"]})
        mark = len(transport.log)
        with pytest.raises(AdapterError, match="range lock"):
            source.apply(self._pinned())
        sent = emitted(transport, mark)
        assert "RANGELCK ON" in sent
        assert "OPER" not in sent
        assert sent[-1] == "RANGELCK OFF"          # not left locked

    def test_standby_releases_the_lock(self):
        source, transport = calibrator(**{
            "OUT?": "1.100000E+02,V,0.000000E+00", "RANGE?": "DC1100V"})
        transport.script("ISR?", stateful_isr(transport))
        source.apply(self._pinned())
        mark = len(transport.log)
        source.standby()
        assert emitted(transport, mark) == ["STBY", "ISR?", "RANGELCK OFF"]

    def test_standby_says_nothing_when_no_lock_was_taken(self):
        source, transport = calibrator()
        transport.script("ISR?", stateful_isr(transport))
        source.apply(Point(Quantity.DCV, 10.0))
        mark = len(transport.log)
        source.standby()
        assert emitted(transport, mark) == ["STBY", "ISR?"]


class TestZeroOutput:
    """M52: the group boundary hands the operator a zeroed output.

    "Press RESET, or 0 V ENTER.  Either action sets the calibrator to 0 mV in
    standby" ([OM 1996] lines 4311-4313, PDF p.102).  Standby alone leaves the
    last setpoint *selected*, one OPR press away.
    """

    def test_zero_output_programs_a_zero(self):
        source, transport = calibrator()
        mark = len(transport.log)
        source.zero_output()
        assert emitted(transport, mark) == [
            "STBY", "ISR?", "OUT 0.0000000000E+00 V, 0.000000E+00 HZ",
            "FAULT?"]

    def test_zero_output_releases_a_range_lock_first(self):
        # 0 V is below every locked range's bottom except the 1100 V range's,
        # where it is a coupled-command fault ([OM 1996] 5-17).
        source, transport = calibrator(**{
            "OUT?": "1.100000E+02,V,0.000000E+00", "RANGE?": "DC1100V"})
        source.apply(Point(Quantity.DCV, 110.0, range_id="DC1100V"))
        mark = len(transport.log)
        source.zero_output()
        sent = emitted(transport, mark)
        assert sent[0] == "RANGELCK OFF"
        assert "OUT 0.0000000000E+00 V, 0.000000E+00 HZ" in sent

    def test_zero_output_leaves_the_wideband_function_first(self):
        """0 V is not a wideband output -- its minimum is 300 uV and it needs
        a frequency -- so a zero after a wideband group would refuse instead
        of de-energizing."""
        source, transport = calibrator(**{
            "OUT?": "1.000000E+00,V,1.000000E+06", "RANGE?": "WB1_1V"})
        transport.script("ISR?", stateful_isr(transport))
        source.apply(Point(Quantity.WBV, 1.0, frequency_hz=1e6))
        mark = len(transport.log)
        source.zero_output()
        sent = emitted(transport, mark)
        assert "WBAND OFF" in sent
        assert sent.index("WBAND OFF") < sent.index(
            "OUT 0.0000000000E+00 V, 0.000000E+00 HZ")

    def test_zero_output_is_the_optional_source_capability_the_engine_calls(self):
        """Not "the attribute exists" -- that it is the one the engine finds.

        ``_de_energize`` looks the capability up by name and calls it before
        every ``standby``; a test that only asserted ``callable(...)`` would
        pass against an adapter whose method the engine could not use.
        """
        from caldmm.cal.engine import _de_energize

        source, transport = calibrator()
        transport.script("ISR?", stateful_isr(transport))
        source.apply(Point(Quantity.DCV, 10.0))
        mark = len(transport.log)
        assert _de_energize(source) == ""          # no zero failure to report
        sent = emitted(transport, mark)
        assert "OUT 0.0000000000E+00 V, 0.000000E+00 HZ" in sent
        assert "STBY" in sent
        assert sent.index("OUT 0.0000000000E+00 V, 0.000000E+00 HZ") \
            < len(sent) - 1


class TestCurrentPost:
    """H02 / H04: which binding posts the current comes out of.

    ``CUR_POST`` is nonvolatile and untouched by ``*RST``, so on any unit
    nobody has set it the current leaves by OUTPUT HI while the operator has
    been told to cable AUX CURRENT OUTPUT.  Reading and setting it is the only
    way the prompt can name the post that is really live.
    """

    def test_the_tokens_mirror_the_driver(self):
        # The import invariant forbids importing the driver at module scope,
        # so the tuple is copied -- and pinned equal here.
        from caldmm.drivers.fluke5700a import CUR_POSTS

        assert CURRENT_POSTS == CUR_POSTS

    def test_current_post_reads_the_calibrator(self):
        source, transport = calibrator(**{"CUR_POST?": "NORMAL"})
        mark = len(transport.log)
        assert source.current_post() == "NORMAL"
        assert emitted(transport, mark) == ["CUR_POST?"]

    def test_set_current_post_writes_and_reads_back(self):
        source, transport = calibrator(**{"CUR_POST?": "AUX"})
        mark = len(transport.log)
        assert source.set_current_post("aux") == "AUX"
        assert emitted(transport, mark) == ["CUR_POST AUX", "FAULT?",
                                            "CUR_POST?"]

    def test_a_post_that_did_not_take_is_an_error(self):
        # A rear-output unit refuses AUX ([OM §4-27, p.114]) and the refusal
        # shows up at the output, not at the command.
        source, transport = calibrator(**{"CUR_POST?": "NORMAL"})
        with pytest.raises(AdapterError, match="CUR_POST"):
            source.set_current_post("AUX")

    def test_an_unknown_post_is_refused_before_any_io(self):
        source, transport = calibrator()
        mark = len(transport.log)
        with pytest.raises(AdapterError, match="NORMAL"):
            source.set_current_post("REAR")
        assert emitted(transport, mark) == []


# ------------------------------------------------------------- environment

class TestEnvironmentProbes:
    def test_static_probe_satisfies_the_protocol(self):
        probe = StaticEnvironment(23.4)
        assert isinstance(probe, EnvironmentProbe)
        assert probe.read().ambient_c == pytest.approx(23.4)
        assert probe.read().source == "operator"

    def test_static_probe_defaults_to_unknown(self):
        # The default must stay "nobody said", which the verdict layer refuses
        # to call a PASS.  A default of 23 C would manufacture the very
        # evidence the temperature gate exists to demand.
        assert StaticEnvironment().read().ambient_c is None




# ----------------------------------------------------------------- factories

class TestFactory:
    def test_parse_instrument_spec(self):
        assert parse_instrument_spec("dmm1:22") == ("dmm1", "22")
        assert parse_instrument_spec("22") == ("22", "22")
        assert parse_instrument_spec("dmm:/dev/ttyUSB0") == \
            ("dmm", "/dev/ttyUSB0")


    def test_registry_matches_every_model_file(self):
        for model_id, cls in ADAPTERS.items():
            spec = load_model(model_id)
            assert spec.adapter, f"{model_id} declares no adapter"
            assert spec.adapter.rsplit(":", 1)[-1] == cls.__name__

    def test_adapter_class_lookup(self):
        assert adapter_class("hp3458a") is HP3458AMeter
        assert adapter_class("fluke5700a") is Fluke5700ASource

    def test_unknown_model_is_refused(self):
        from caldmm.cal.specs import SpecError

        with pytest.raises((AdapterError, SpecError, FileNotFoundError)):
            adapter_class("keithley2000")

    def test_open_meter_refuses_a_source(self):
        with pytest.raises(AdapterError, match="is a source"):
            open_meter("fluke5700a", "cal:4")

    def test_open_source_refuses_a_meter(self):
        with pytest.raises(AdapterError, match="is a meter"):
            open_source("hp3458a", "dmm:22")

    def test_open_meter_builds_the_driver_from_the_address(self, monkeypatch):
        built = {}

        class _Fake:
            def __init__(self, **kwargs):
                built.update(kwargs)

        import caldmm.drivers.hp3458a as hp3458a
        monkeypatch.setattr(hp3458a, "HP3458A", _Fake)
        meter = open_meter("hp3458a", "dmm1:22", nplc=100)
        assert built == {"gpib_address": 22, "gpib_bus": 0,
                         "name": "dmm1", "nplc": 100}
        assert isinstance(meter, HP3458AMeter)
        assert meter.model_id == "hp3458a"

    def test_open_source_builds_the_calibrator(self, monkeypatch):
        built = {}

        class _Fake:
            def __init__(self, **kwargs):
                built.update(kwargs)

        import caldmm.drivers.fluke5700a as fluke5700a
        monkeypatch.setattr(fluke5700a, "Fluke5700A", _Fake)
        source = open_source("fluke5700a", "5700a:4")
        assert built == {"gpib_address": 4, "gpib_bus": 0, "name": "5700a"}
        assert isinstance(source, Fluke5700ASource)

    def test_open_meter_with_a_bus_qualified_address(self, monkeypatch):
        # Two USB-GPIB adapters on one bench: the 3458A on /dev/gpib1 while
        # the 5700A sits on /dev/gpib0. 'name:bus,pad' expresses that.
        built = {}

        class _Fake:
            def __init__(self, **kwargs):
                built.update(kwargs)

        import caldmm.drivers.hp3458a as hp3458a
        monkeypatch.setattr(hp3458a, "HP3458A", _Fake)
        open_meter("hp3458a", "dmm1:1,22")
        assert built["gpib_address"] == 22
        assert built["gpib_bus"] == 1

    def test_open_source_with_a_bus_qualified_address(self, monkeypatch):
        # The 'gpib0,4' sugar — the spelling cal_verify's own error text
        # has always suggested.
        built = {}

        class _Fake:
            def __init__(self, **kwargs):
                built.update(kwargs)

        import caldmm.drivers.fluke5700a as fluke5700a
        monkeypatch.setattr(fluke5700a, "Fluke5700A", _Fake)
        open_source("fluke5700a", "5700a:gpib1,4")
        assert built["gpib_address"] == 4
        assert built["gpib_bus"] == 1


class TestParseGpibAddress:
    def test_bare_pad_lands_on_bus_zero(self):
        assert parse_gpib_address("22") == (0, 22)

    def test_bus_qualified(self):
        assert parse_gpib_address("1,22") == (1, 22)

    def test_gpib_prefix_sugar(self):
        assert parse_gpib_address("gpib1,22") == (1, 22)
        assert parse_gpib_address("GPIB0,4") == (0, 4)

    def test_whitespace_tolerated(self):
        assert parse_gpib_address(" 1,4 ") == (1, 4)

    @pytest.mark.parametrize("bad", [
        "", "abc", "1,2,3", "gpib,4", "1,", ",4", "4.5", "1:22",
    ])
    def test_malformed_is_refused(self, bad):
        with pytest.raises(AdapterError):
            parse_gpib_address(bad)

    @pytest.mark.parametrize("bad", ["31", "-1", "1,31"])
    def test_pad_outside_0_30_is_refused(self, bad):
        # 31 is the unlisten/untalk address; nothing answers there.
        with pytest.raises(AdapterError):
            parse_gpib_address(bad)

    def test_negative_bus_is_refused(self):
        with pytest.raises(AdapterError):
            parse_gpib_address("-1,4")


class TestParseSerialAddress:
    """``/dev/ttyUSB0`` and ``/dev/ttyUSB0@19200`` for a serial 5700A."""

    def test_a_bare_path_takes_the_factory_baud(self):
        assert parse_serial_address("/dev/ttyUSB0") == ("/dev/ttyUSB0", 9600)

    def test_an_explicit_baud(self):
        assert parse_serial_address("/dev/ttyUSB0@19200") == (
            "/dev/ttyUSB0", 19200)

    def test_whitespace_tolerated(self):
        assert parse_serial_address(" /dev/ttyS0@1200 ") == (
            "/dev/ttyS0", 1200)

    @pytest.mark.parametrize("bad", ["/dev/ttyUSB0@57600", "/dev/ttyUSB0@0"])
    def test_a_rate_the_calibrator_lacks_is_refused(self, bad):
        # SP_SET has no such rate, and a mismatched link does not fail
        # loudly — it just never answers.
        with pytest.raises(AdapterError) as excinfo:
            parse_serial_address(bad)
        assert "19200" in str(excinfo.value)

    def test_a_non_numeric_baud_is_refused(self):
        with pytest.raises(AdapterError):
            parse_serial_address("/dev/ttyUSB0@fast")

    @pytest.mark.parametrize("address,serial", [
        ("/dev/ttyUSB0", True),
        ("/dev/ttyUSB0@19200", True),
        ("4", False),
        ("gpib1,4", False),
        ("1,22", False),
    ])
    def test_the_slash_is_what_distinguishes_the_two_grammars(
            self, address, serial):
        assert is_serial_address(address) is serial

    def test_the_baud_table_matches_the_driver(self):
        # Mirrored, not imported: cal/ may only touch drivers lazily inside a
        # function.  A copy that drifts silently accepts a rate the port
        # cannot be set to.
        from caldmm.drivers import fluke5700a as f57

        from caldmm.cal import adapters

        assert adapters.SERIAL_BAUD_RATES == f57.SERIAL_BAUD_RATES
        assert adapters.DEFAULT_SERIAL_BAUD == f57.DEFAULT_SERIAL_BAUD


class TestSourceOpensEitherInterface:
    """``Fluke5700ASource.open`` dispatches on the address spelling."""

    def _opened(self, address, **kwargs):
        with patch("caldmm.drivers.fluke5700a.Fluke5700A") as driver:
            Fluke5700ASource.open(address, **kwargs)
        return driver.call_args.kwargs

    def test_a_gpib_address_opens_gpib(self):
        kwargs = self._opened("gpib1,4", name="5700a")
        assert (kwargs["gpib_bus"], kwargs["gpib_address"]) == (1, 4)
        assert "serial_port" not in kwargs

    def test_a_device_path_opens_serial(self):
        kwargs = self._opened("/dev/ttyUSB0@19200", name="5700a")
        assert kwargs["serial_port"] == "/dev/ttyUSB0"
        assert kwargs["serial_baud"] == 19200
        assert "gpib_address" not in kwargs


# --------------------------------------------------- MultimeterBase hooks


# ----------------------------------------------------------------- invariants

IMPORT_HYGIENE_SCRIPT = textwrap.dedent("""\
    import sys

    BLOCKED = {"serial", "Gpib", "pyvisa", "smbus", "smbus2", "psycopg"}

    for name in list(sys.modules):
        if name.split(".")[0] in BLOCKED:
            del sys.modules[name]

    class Blocker:
        def find_spec(self, fullname, path=None, target=None):
            if fullname.split(".")[0] in BLOCKED:
                raise ImportError("blocked for this test: " + fullname)
            return None

    sys.meta_path.insert(0, Blocker())

    import caldmm.cal.adapters as adapters

    leaked = sorted(m for m in sys.modules if m.startswith("caldmm.drivers"))
    assert not leaked, "adapters imported drivers eagerly: " + repr(leaked)

    # Everything that does not need an instrument still works.
    from caldmm.cal.quantities import Connection, Quantity
    from caldmm.cal.specs import load_model
    assert adapters.func_name(load_model("hp3458a"), Quantity.RES,
                              Connection.FOUR_WIRE) == "OHMF"
    assert adapters.adapter_class("hp3458a").__name__ == "HP3458AMeter"
    assert adapters.StaticEnvironment(23.0).read().ambient_c == 23.0

    leaked = sorted(m for m in sys.modules if m.startswith("caldmm.drivers"))
    assert not leaked, "offline path pulled in drivers: " + repr(leaked)
    print("OK")
    """)


def test_adapters_import_their_drivers_lazily():
    """``import caldmm.cal.adapters`` must not touch a driver.

    In a clean subprocess with linux-gpib, pyserial, PyVISA and psycopg all
    blocked, so that the engine and the plan generator stay importable on a
    machine with no lab attached.

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


class TestProbeConfiguration:
    """configure_probe(): same function and range, read fast (2026-11-01)."""


    def test_a_low_plan_nplc_is_not_raised(self):
        from caldmm.cal.adapters import MeterAdapter

        knobs = MeterAdapter._probe_knobs(None, {"nplc": 1.0}, Quantity.DCV,
                                          0.0)
        assert knobs["nplc"] == 1.0




class TestWindowsSerialPorts:
    """``COM3`` / ``COM3@19200`` name a serial port on Windows."""

    @pytest.mark.parametrize("address", ["COM3", "com12", "COM3@19200"])
    def test_com_ports_are_serial(self, address):
        from caldmm.cal.adapters import is_serial_address
        assert is_serial_address(address)

    @pytest.mark.parametrize("address", ["4", "1,22", "gpib1,4", "COMX"])
    def test_gpib_spellings_are_not(self, address):
        from caldmm.cal.adapters import is_serial_address
        assert not is_serial_address(address)

    def test_com_port_with_baud(self):
        from caldmm.cal.adapters import parse_serial_address
        assert parse_serial_address("COM3@19200") == ("COM3", 19200)
        assert parse_serial_address("COM3")[0] == "COM3"


class TestHP3458AAdjustmentTemperatures:
    """CAL? 58/59/60 and ACAL: the Cal Manual's preconditions (p.57)."""

    def test_cal_temperatures_reads_the_three_constants(self):
        device = MagicMock()
        device.get_cal_const.side_effect = lambda n: {58: "23.1", 59: "23.4",
                                                      60: "23.9"}[n]
        meter = HP3458AMeter(device, model_id="hp3458a")
        assert meter.cal_temperatures() == {"cal0": 23.1, "dcv": 23.4,
                                            "ohms": 23.9}

    def test_an_unreadable_constant_is_left_out(self):
        device = MagicMock()
        device.get_cal_const.side_effect = lambda n: (
            "23.4" if n == 59 else (_ for _ in ()).throw(OSError("timeout")))
        meter = HP3458AMeter(device, model_id="hp3458a")
        assert meter.cal_temperatures() == {"dcv": 23.4}

    def test_autocal_sends_acal_all(self):
        from caldmm.drivers.hp3458a import ACAL
        device = MagicMock()
        HP3458AMeter(device, model_id="hp3458a").autocal("all")
        device.acal.assert_called_once_with(ACAL.ALL)

"""Tests for HP 3458A burst-capture (internal-MEM) path."""

import pytest
from unittest.mock import patch, call
from caldmm.drivers.hp3458a import (
    HP3458A,
    Func,
    _OCOMP_TIME_FACTOR,
    _expected_conversion_sec,
)


@pytest.fixture
def gpib_mock():
    """Patches Gpib.Gpib. Default read() returns b'0\r\n'; serial_poll
    returns 0 (no stale data) so burst_capture's drain skips."""
    with patch('caldmm.drivers.hp3458a.Gpib.Gpib') as MockGpib:
        instance = MockGpib.return_value
        instance.read.return_value = b'0\r\n'
        instance.serial_poll.return_value = 0x00
        yield instance


def _written_cmds(gpib_mock):
    return [c.args[0] for c in gpib_mock.write.call_args_list]


class TestHasBurst:
    def test_hp3458a_advertises_burst(self, gpib_mock):
        dev = HP3458A()
        assert dev.has_burst() is True


class TestOffsetCompensatedOhmsNeverBursts:
    """A 4-wire resistance verification aborted on its first point with
    ``3458A burst: 0 in MEM + 1 leaked != 2 requested``, and the readings
    never arrived in MEM however long it waited (bench, 2026-08-23, unit
    dmm1: OHMF, NPLC 100, AZERO ON, OCOMP ON, 19 Ohm, still 0 in MEM
    after 180 s).  ``read_single_sample`` reads the same point in 23.17 s,
    so ``read_samples`` must loop over it instead of bursting.
    """

    def test_ocomp_ohms_withdraws_the_burst_capability(self, gpib_mock):
        dev = HP3458A(func=Func.OHMF, ocomp=True, max_input=10.0)
        assert dev.ocomp_active() is True
        assert dev.has_burst() is False

    def test_two_wire_ohms_is_withdrawn_too(self, gpib_mock):
        dev = HP3458A(func=Func.OHM, ocomp=True, max_input=10.0)
        assert dev.has_burst() is False

    def test_dcv_bursts_even_with_ocomp_left_on(self, gpib_mock):
        # OCOMP is a resistance technique; leaving it on costs a dc point
        # nothing, so withdrawing the burst path there would be a pure loss.
        dev = HP3458A(func=Func.DCV, ocomp=True, max_input=10.0)
        assert dev.ocomp_active() is False
        assert dev.has_burst() is True

    def test_read_samples_never_reaches_burst_capture(self, gpib_mock):
        """The regression proper: read_samples() must not route an
        offset-compensated ohms request into burst_capture()."""
        dev = HP3458A(func=Func.OHMF, ocomp=True, max_input=10.0)
        calls = []

        def _burst(n, **kwargs):
            calls.append(n)
            raise AssertionError('burst_capture must not be reached')

        dev.burst_capture = _burst
        dev.read_single_sample = lambda: 18.999116
        assert dev.read_samples(4) == [18.999116] * 4
        assert calls == []

    def test_read_samples_still_bursts_on_dcv(self, gpib_mock):
        dev = HP3458A(func=Func.DCV, ocomp=True, max_input=10.0)
        dev.burst_capture = lambda n, **kwargs: [float(i) for i in range(n)]
        assert dev.read_samples(3) == [0.0, 1.0, 2.0]

    def test_burst_capture_refuses_rather_than_timing_out(self, gpib_mock):
        """Direct callers (apps/dmm_bias.py, tools/dmm_noise.py) bypass the
        capability hook; they get a legible refusal, not three minutes of
        polling MCOUNT? for readings that are never coming."""
        dev = HP3458A(func=Func.OHMF, ocomp=True, max_input=10.0)
        with pytest.raises(RuntimeError, match='burst refused'):
            dev.burst_capture(2)

    def test_the_refusal_happens_before_any_gpib_traffic(self, gpib_mock):
        dev = HP3458A(func=Func.OHMF, ocomp=True, max_input=10.0)
        gpib_mock.write.reset_mock()
        with pytest.raises(RuntimeError):
            dev.burst_capture(2)
        assert _written_cmds(gpib_mock) == []


class TestResistanceNeverBursts:
    """Withdrawing burst only for *offset-compensated* ohms was too narrow.

    A 4-wire verification then reached its first uncompensated point --
    190 kOhm, which measures on the 1 MOhm range, where the 3458A offers no
    offset compensation at all (Programming Guide :238, 10 Ohm to 100 kOhm
    only) -- and aborted there with the same
    ``0 in MEM + 1 leaked != 2 requested``.  Sourcing a real 190 kOhm from a
    5700A, 4-wire, ``OCOMP OFF``, ``AZERO ON`` (bench, 2026-08-24, unit
    dmm1):

    ======== =========== ================================================
    NPLC 10  burst of 2  OK, 1.50 s, ['189986.96', '189989.52']
    NPLC 10  burst of 4  OK, 3.61 s, four good values
    NPLC 100 burst of 2  FAILED, 0 in MEM + 1 leaked, after 120.0 s
    NPLC 100 single      read_single_sample() OK, 4.07 s / 4.64 s
    ======== =========== ================================================

    against DCV at NPLC 100 in bursts of five completing 15/15 points.
    So it is the
    combination -- a resistance function *and* a high integration time --
    and not OCOMP, the deadline, NPLC alone or the function alone.  120 s is
    ~30x the 4 s one reading costs at that setting, so no timeout can fix it.

    The rule is written on the function because the boundary lies somewhere
    in (10, 100] and no setting in that interval has been measured;
    ``cal/data/hp3458a.yaml`` pins RES to ``nplc: 100`` anyway, so ohms is
    always at the failing setting in practice.
    """

    def test_uncompensated_four_wire_ohms_withdraws_burst(self, gpib_mock):
        dev = HP3458A(func=Func.OHMF, nplc=100, ocomp=False, max_input=10.0)
        assert dev.ocomp_active() is False       # the 1 MOhm range has none
        assert dev.has_burst() is False

    def test_uncompensated_two_wire_ohms_withdraws_burst(self, gpib_mock):
        dev = HP3458A(func=Func.OHM, nplc=100, ocomp=False, max_input=10.0)
        assert dev.has_burst() is False

    def test_ohms_is_withdrawn_at_the_nplc_that_works_too(self, gpib_mock):
        """NPLC 10 ohms bursts fine on the bench; the hook still declines it.

        Deliberate: the alternative is an NPLC threshold picked out of an
        interval nobody has measured.  What is given up is the automatic
        choice, not the capability -- see
        :meth:`test_a_deliberate_low_nplc_ohms_burst_is_still_allowed`.
        """
        dev = HP3458A(func=Func.OHMF, nplc=10, ocomp=False, max_input=10.0)
        assert dev.has_burst() is False

    def test_selecting_a_function_flips_the_capability(self, gpib_mock):
        dev = HP3458A(func=Func.DCV, max_input=10.0)
        assert dev.has_burst() is True
        dev.set_func(Func.OHMF, max_input=10.0)
        assert dev.has_burst() is False
        dev.set_func(Func.DCV, max_input=10.0)
        assert dev.has_burst() is True

    def test_a_resistance_point_never_reaches_burst_capture(self, gpib_mock):
        """The regression proper, at the point that actually aborted: a
        190 kOhm 4-wire reading at NPLC 100 with no offset compensation must
        come out of ``read_single_sample``, not out of MEM."""
        dev = HP3458A(func=Func.OHMF, nplc=100, ocomp=False, max_input=10.0)

        def _burst(n, **kwargs):
            raise AssertionError('burst_capture must not be reached')

        dev.burst_capture = _burst
        dev.read_single_sample = lambda: 189986.96
        assert dev.read_samples(2) == [189986.96, 189986.96]

    def test_a_deliberate_low_nplc_ohms_burst_is_still_allowed(self,
                                                              gpib_mock):
        """``burst_capture`` is narrower than the hook on purpose: a caller
        that asks for an ohms burst by name gets the path the bench showed
        working at NPLC 10, rather than a refusal for a configuration nobody
        has seen fail.  Only the OCOMP combination is refused outright."""
        dev = HP3458A(func=Func.OHMF, nplc=10, ocomp=False, max_input=10.0)
        gpib_mock.write.reset_mock()
        gpib_mock.read.side_effect = [
            b'2\r\n',                       # MCOUNT?
            b'+1.8998696E+05\r\n',          # RMEM reading 1
            b'+1.8998952E+05\r\n',          # RMEM reading 2
        ]
        with patch('caldmm.drivers.hp3458a.time.sleep'):
            out = dev.burst_capture(2)
        assert out == [189986.96, 189989.52]
        assert 'MEM FIFO' in _written_cmds(gpib_mock)


class TestApertureEstimate:
    """``_expected_conversion_sec`` was characterized on DCV, where OCOMP
    does nothing, so every timeout derived from it was ~6.3x short for
    offset-compensated ohms."""

    def test_dcv_is_the_tabulated_conversion_time(self, gpib_mock):
        dev = HP3458A(func=Func.DCV, nplc=100, delay=0.0, ocomp=True,
                      max_input=10.0)
        assert dev.expected_sample_sec() == pytest.approx(3.70)

    def test_ocomp_ohms_covers_the_measured_reading_time(self, gpib_mock):
        # Bench: 23.17 s per reading at NPLC 100 with OCOMP ON, against the
        # 3.70 s the table alone predicts.  The estimate has to reach it.
        dev = HP3458A(func=Func.OHMF, nplc=100, delay=0.0, ocomp=True,
                      max_input=10.0)
        estimate = dev.expected_sample_sec()
        assert estimate == pytest.approx(3.70 * _OCOMP_TIME_FACTOR)
        assert estimate >= 23.17

    def test_the_factor_is_at_least_the_measured_ratio(self):
        assert _OCOMP_TIME_FACTOR >= 23.17 / 3.69

    def test_the_programmed_delay_is_added_once_per_reading(self, gpib_mock):
        dev = HP3458A(func=Func.OHMF, nplc=100, delay=1.0, ocomp=True,
                      max_input=10.0)
        assert dev.expected_sample_sec() == pytest.approx(
            _expected_conversion_sec(100) * _OCOMP_TIME_FACTOR + 1.0)

    def test_automatic_delay_contributes_nothing(self, gpib_mock):
        # DELAY -1 is the meter's automatic settling; it is never reported,
        # so it is counted as zero rather than guessed at.  The 1.5x + 10 s
        # headroom the callers add is what covers it.
        dev = HP3458A(func=Func.DCV, nplc=100, delay=-1, max_input=10.0)
        assert dev.expected_sample_sec() == pytest.approx(3.70)

    def test_the_burst_timeout_is_built_from_the_estimate(self, gpib_mock):
        # Non-OCOMP, so the burst path is still available; the delay a
        # resistance point now programs has to widen the budget with it.
        dev = HP3458A(func=Func.OHMF, nplc=100, delay=1.0, ocomp=False,
                      max_input=10.0)
        aperture = dev.expected_sample_sec()
        assert aperture == pytest.approx(4.70)
        # A clock that jumps straight past any deadline, so the arithmetic is
        # asserted from the message rather than by waiting the budget out.
        clock = iter([0.0] + [1.0e6] * 8)
        with patch('caldmm.drivers.hp3458a.time.sleep'), \
                patch('caldmm.drivers.hp3458a.time.monotonic',
                      side_effect=lambda: next(clock)), \
                patch.object(HP3458A, 'mcount', return_value=0):
            with pytest.raises(TimeoutError) as excinfo:
                dev.burst_capture(4, timeout_sec=None)
        # n * aperture * 1.5 + 10
        assert f'{4 * aperture * 1.5 + 10.0:.1f} s' in str(excinfo.value)

    def test_the_single_read_timeout_outgrows_the_flat_ceiling(self, gpib_mock):
        # read_timeout_sec is a flat 60 s that predates any estimate; at
        # NPLC 200 with OCOMP ON one legitimate reading takes ~46 s, and the
        # polling budget must not be shorter than the reading.
        dev = HP3458A(func=Func.OHMF, nplc=200, delay=1.0, ocomp=True,
                      max_input=10.0)
        expected = dev.expected_sample_sec()
        assert expected > 45.0
        assert dev._read_timeout_sec(expected) > expected
        assert dev._read_timeout_sec(expected) > dev.read_timeout_sec

    def test_a_fast_configuration_keeps_the_flat_ceiling(self, gpib_mock):
        dev = HP3458A(func=Func.DCV, nplc=10, delay=0.0, max_input=10.0)
        assert dev._read_timeout_sec(dev.expected_sample_sec()) == 60.0

    def test_a_raised_ceiling_is_still_honoured(self, gpib_mock):
        dev = HP3458A(func=Func.DCV, nplc=10, delay=0.0, max_input=10.0)
        dev.read_timeout_sec = 600.0
        assert dev._read_timeout_sec(dev.expected_sample_sec()) == 600.0


class TestSetMformat:
    def test_valid_formats_send_mformat_command(self, gpib_mock):
        dev = HP3458A()
        gpib_mock.write.reset_mock()
        for fmt in ('ASCII', 'SINT', 'DINT', 'SREAL', 'DREAL'):
            dev.set_mformat(fmt)
        cmds = _written_cmds(gpib_mock)
        assert cmds == ['MFORMAT ASCII', 'MFORMAT SINT', 'MFORMAT DINT',
                        'MFORMAT SREAL', 'MFORMAT DREAL']

    def test_lowercase_accepted(self, gpib_mock):
        dev = HP3458A()
        gpib_mock.write.reset_mock()
        dev.set_mformat('ascii')
        assert _written_cmds(gpib_mock) == ['MFORMAT ASCII']

    def test_invalid_format_raises(self, gpib_mock):
        dev = HP3458A()
        with pytest.raises(ValueError, match='MFORMAT'):
            dev.set_mformat('BOGUS')


class TestSetMem:
    def test_valid_modes_send_mem_command(self, gpib_mock):
        dev = HP3458A()
        gpib_mock.write.reset_mock()
        for mode in ('OFF', 'FIFO', 'LIFO', 'CONT'):
            dev.set_mem(mode)
        cmds = _written_cmds(gpib_mock)
        assert cmds == ['MEM OFF', 'MEM FIFO', 'MEM LIFO', 'MEM CONT']

    def test_invalid_mode_raises(self, gpib_mock):
        dev = HP3458A()
        with pytest.raises(ValueError, match='MEM mode'):
            dev.set_mem('BOGUS')


class TestSetNrdgs:
    def test_default_source_is_auto(self, gpib_mock):
        dev = HP3458A()
        gpib_mock.write.reset_mock()
        dev.set_nrdgs(42)
        assert _written_cmds(gpib_mock) == ['NRDGS 42,AUTO']

    def test_custom_source(self, gpib_mock):
        dev = HP3458A()
        gpib_mock.write.reset_mock()
        dev.set_nrdgs(10, source='TIMER')
        assert _written_cmds(gpib_mock) == ['NRDGS 10,TIMER']

    def test_zero_count_raises(self, gpib_mock):
        dev = HP3458A()
        with pytest.raises(ValueError, match='count must be'):
            dev.set_nrdgs(0)

    def test_invalid_source_raises(self, gpib_mock):
        dev = HP3458A()
        with pytest.raises(ValueError, match='source'):
            dev.set_nrdgs(5, source='BOGUS')


class TestMcount:
    def test_parses_integer_response(self, gpib_mock):
        dev = HP3458A()
        gpib_mock.read.return_value = b'  327  \r\n'
        gpib_mock.write.reset_mock()
        assert dev.mcount() == 327
        assert _written_cmds(gpib_mock) == ['MCOUNT?']


class TestRecallMem:
    def test_recovers_n_floats_from_line_per_read(self, gpib_mock):
        """Firmware that emits one EOI-terminated reading per read()."""
        dev = HP3458A()
        gpib_mock.read.side_effect = [
            b'+1.0000000E-06\r\n',
            b'+2.0000000E-06\r\n',
            b'+3.0000000E-06\r\n',
        ]
        gpib_mock.write.reset_mock()
        out = dev.recall_mem(start=1, n=3)
        assert out == [1.0e-6, 2.0e-6, 3.0e-6]
        assert _written_cmds(gpib_mock) == ['RMEM 1,3,1']

    def test_recovers_n_floats_from_comma_chunk(self, gpib_mock):
        """Firmware that batches multiple readings in a single read()."""
        dev = HP3458A()
        gpib_mock.read.side_effect = [
            b'+1.0E-06,+2.0E-06,+3.0E-06\r\n',
        ]
        gpib_mock.write.reset_mock()
        out = dev.recall_mem(start=1, n=3)
        assert out == [1.0e-6, 2.0e-6, 3.0e-6]

    def test_zero_n_raises(self, gpib_mock):
        dev = HP3458A()
        with pytest.raises(ValueError):
            dev.recall_mem(start=1, n=0)


class TestBurstCapture:
    def test_rejects_oversize_request(self, gpib_mock):
        dev = HP3458A()
        with pytest.raises(ValueError, match='exceeds'):
            dev.burst_capture(10_000)

    def test_rejects_zero(self, gpib_mock):
        dev = HP3458A()
        with pytest.raises(ValueError):
            dev.burst_capture(0)

    def test_full_burst_sequence_against_mock(self, gpib_mock):
        """End-to-end: configure → arm → poll MCOUNT → recall → restore."""
        dev = HP3458A(nplc=1)  # short aperture so test runs fast
        gpib_mock.write.reset_mock()

        # The driver issues MCOUNT? first (poll), then RMEM. read() is
        # polled in order: count response, then N reading lines.
        n = 5
        reading_lines = [f'{(i+1)*1e-6:+.7E}\r\n'.encode('ascii')
                         for i in range(n)]
        gpib_mock.read.side_effect = [b'5\r\n'] + reading_lines

        out = dev.burst_capture(n)

        assert out == [1.0e-6, 2.0e-6, 3.0e-6, 4.0e-6, 5.0e-6]

        cmds = _written_cmds(gpib_mock)
        # Setup phase
        assert 'TARM HOLD' in cmds
        assert 'MFORMAT ASCII' in cmds
        assert 'MEM FIFO' in cmds
        assert 'NRDGS 5,AUTO' in cmds
        assert 'TARM SGL' in cmds
        # Status + fetch
        assert 'MCOUNT?' in cmds
        assert 'RMEM 1,5,1' in cmds
        # Teardown phase (restore steady-state)
        assert 'NRDGS 1,AUTO' in cmds
        assert 'MEM OFF' in cmds
        # TARM should be restored to HOLD (default non-freerunning)
        teardown_idx = cmds.index('NRDGS 1,AUTO')
        assert 'TARM HOLD' in cmds[teardown_idx:]

    def test_burst_restores_state_on_timeout(self, gpib_mock):
        """If MCOUNT? never reports completion, the teardown still runs."""
        dev = HP3458A(nplc=1)
        gpib_mock.write.reset_mock()
        gpib_mock.read.return_value = b'0\r\n'  # MCOUNT always 0

        with pytest.raises(TimeoutError):
            dev.burst_capture(3, timeout_sec=0.5)

        cmds = _written_cmds(gpib_mock)
        # Teardown writes must appear after the setup writes
        assert 'NRDGS 1,AUTO' in cmds
        assert 'MEM OFF' in cmds

    def test_burst_drains_stale_output_buffer(self, gpib_mock):
        """A stale reading left by a prior config change must be consumed."""
        dev = HP3458A(nplc=1)
        gpib_mock.write.reset_mock()
        # First serial_poll (pre-burst): data-available. Subsequent polls:
        # no data (no leak during burst).
        polls = [0x80] + [0x00] * 20
        gpib_mock.serial_poll.side_effect = polls

        n = 3
        gpib_mock.read.side_effect = [
            b'+9.9E-07\r\n',          # stale reading drained at burst start
            b'3\r\n',                 # MCOUNT?
            b'+1.0E-06\r\n',          # RMEM reading 1
            b'+2.0E-06\r\n',          # RMEM reading 2
            b'+3.0E-06\r\n',          # RMEM reading 3
        ]
        out = dev.burst_capture(n)
        assert out == [1.0e-6, 2.0e-6, 3.0e-6]
        # The serial_poll happened (drain check)
        assert gpib_mock.serial_poll.called

    def test_burst_recovers_leaked_first_sample(self, gpib_mock):
        """Firmware quirk: first burst reading leaks into output buffer
        despite MEM FIFO. The driver must capture it and reconcile MCOUNT."""
        dev = HP3458A(nplc=1)
        gpib_mock.write.reset_mock()
        # Pre-burst: no stale. Post-burst polling: 1 leak available, then none.
        polls = [0x00, 0x80, 0x00, 0x00, 0x00, 0x00]
        gpib_mock.serial_poll.side_effect = polls

        n = 3
        # In order: leak (1), MCOUNT? (returns 2 → matches needed_in_mem=2),
        # RMEM 1,2,1 returns 2 readings.
        gpib_mock.read.side_effect = [
            b'+1.0E-06\r\n',           # leaked first reading
            b'2\r\n',                  # MCOUNT? → 2 in MEM (matches n-leaks=2)
            b'+2.0E-06\r\n',           # RMEM reading 1
            b'+3.0E-06\r\n',           # RMEM reading 2
        ]
        out = dev.burst_capture(n)
        assert out == [1.0e-6, 2.0e-6, 3.0e-6]

    def test_burst_restores_freerunning_state(self, gpib_mock):
        """When the instrument was freerunning, burst restores TARM AUTO."""
        dev = HP3458A(freerunning=True, nplc=1)
        gpib_mock.write.reset_mock()

        n = 2
        gpib_mock.read.side_effect = [
            b'2\r\n',                # MCOUNT?
            b'+1.0E-06\r\n',         # reading 1
            b'+2.0E-06\r\n',         # reading 2
        ]
        out = dev.burst_capture(n)
        assert out == [1.0e-6, 2.0e-6]

        cmds = _written_cmds(gpib_mock)
        # Last TARM write should restore AUTO (freerunning was True)
        tarm_writes = [c for c in cmds if c.startswith('TARM ')]
        assert tarm_writes[-1] == 'TARM AUTO'
        assert dev.freerunning is True


class TestBaseFallback:
    def test_non_burst_driver_raises_nice_error(self):
        from caldmm.drivers._base import MultimeterBase

        class FakeDmm(MultimeterBase):
            def cmd(self, c): pass
            def query(self, c): return ''
            def read_raw_str(self): return ''
            def read_single_sample(self): return 0.0
            def set_func(self, func, max_input=-1): pass
            @property
            def nplc(self): return 1
            @nplc.setter
            def nplc(self, v): pass
            @property
            def delay(self): return 0.0
            @delay.setter
            def delay(self, v): pass
            def display_on(self): pass
            def display_off(self): pass

        dev = FakeDmm()
        assert dev.has_burst() is False
        with pytest.raises(NotImplementedError, match='burst'):
            dev.burst_capture(10)


class TestAcFunctionsReadOneAtATime:
    """An ACV verification aborted at its wiring proof with ``3458A burst:
    0 in MEM + 0 leaked != 2 requested after 12.6 s`` (measured:
    SETACV SYNC, RES 0.001, 10 mV range, 1 kHz).  The burst
    budget came from the DCV integration table; a 7.5-digit SYNC reading
    takes tens of seconds.  The proven ac path is ``read_single_sample``.
    """

    @pytest.mark.parametrize('func', [Func.ACV, Func.ACDCV, Func.ACI,
                                      Func.ACDCI])
    def test_ac_functions_withdraw_burst(self, gpib_mock, func):
        dev = HP3458A(func=func, max_input=10.0)
        assert dev.has_burst() is False

    def test_ac_budget_is_the_slowest_datasheet_rate(self, gpib_mock):
        dev = HP3458A(func=Func.ACV, nplc=10, max_input=10.0)
        assert dev.expected_sample_sec() >= 40.0
        # read_single_sample's ceiling must exceed one 40 s reading.
        assert dev._read_timeout_sec(dev.expected_sample_sec()) > 40.0

    def test_dcv_budget_is_unchanged(self, gpib_mock):
        dev = HP3458A(func=Func.DCV, nplc=10, max_input=10.0)
        assert dev.expected_sample_sec() == pytest.approx(
            _expected_conversion_sec(10) + max(dev._delay, 0.0))


class TestDciReadsOneAtATime:
    """Measured, input open, OCOMP OFF: a DCI burst of 2
    worked at NPLC 10 and failed at NPLC 100 with ``0 in MEM + 1 leaked``,
    the ohms signature; DCV burst at both.  ``read_single_sample`` read DCI
    at NPLC 100 in 4.2 s."""

    def test_dci_withdraws_burst(self, gpib_mock):
        dev = HP3458A(func=Func.DCI, nplc=100, ocomp=False, max_input=1e-3)
        assert dev.has_burst() is False

    def test_dcv_still_bursts(self, gpib_mock):
        dev = HP3458A(func=Func.DCV, nplc=100, ocomp=False, max_input=10.0)
        assert dev.has_burst() is True

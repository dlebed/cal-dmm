"""Tests for HP 3458A *external* calibration adjustment — the ``CAL`` command.

Kept separate from ``test_hp3458a_burst.py`` because this is the only 3458A
code path that writes non-volatile calibration memory, and because the
distinction under test — ``CAL`` (external adjustment) is not ``ACAL``
(internal self-calibration) — is the one a reader most often gets wrong.
"""

import pytest
from unittest.mock import patch

from caldmm.drivers.hp3458a import (
    ACAL, HP3458A,
    CalibrationError, CalibrationLockoutError, CalibrationWriteNotAllowed,
    adjustment_kind, expected_adjustment_seconds,
    AUXERR_CAL_RAM_WRITE_PROTECTION, AUXERR_CAL_VALUE_OUT_OF_RANGE,
    ERR_CALIBRATION, ERR_HARDWARE,
)

SECRET = 3458


class FakeGpib:
    """A real fake for ``Gpib.Gpib`` that answers 3458A queries.

    Routes by command rather than by call order, so a test asserts the
    *sequence the driver emitted* instead of encoding the answer in a
    ``side_effect`` list. ``read()`` returns bytes, as linux-gpib does — a
    bare ``MagicMock`` would hand back a ``MagicMock`` that does not survive
    ``float()``.
    """

    def __init__(self):
        self.written = []
        self.err = 0
        self.auxerr = 0
        # Errors the emulated meter raises *in response to* the next write
        # command. The driver clears ERR?/AUXERR? before adjusting, so an
        # error seeded into ``err`` directly would be consumed by that
        # pre-clear instead of reaching the post-adjustment check.
        self.err_on_write = 0
        self.auxerr_on_write = 0
        self.calnum = 17
        self.cal_string = 'CALIBRATED 2026-08-01'
        self.stale_reading = False
        self.silent_refusal = False    # accept CAL but store nothing (lockout)
        self._pending = []

    # -- Gpib.Gpib surface ----------------------------------------------
    def write(self, cmd):
        self.written.append(cmd)
        self._dispatch(cmd.strip())

    def read(self):
        return self._pending.pop(0) if self._pending else b'0\r\n'

    def clear(self):
        pass

    def serial_poll(self):
        return 0x80 if self.stale_reading else 0x00

    # -- instrument emulation -------------------------------------------
    def _dispatch(self, cmd):
        if cmd == 'ERR?':
            self._pending.append(f'{self.err}\r\n'.encode())
            self.err = 0                       # ERR? reads *and clears*
        elif cmd == 'AUXERR?':
            self._pending.append(f'{self.auxerr}\r\n'.encode())
            self.auxerr = 0
        elif cmd == 'CALNUM?':
            self._pending.append(f'{self.calnum}\r\n'.encode())
        elif cmd == 'CALSTR?':
            self._pending.append(f'{self.cal_string}\r\n'.encode())
        elif (cmd.startswith('CAL ') or cmd.startswith('CALSTR ')
                or cmd.startswith('SECURE ')) and not cmd.endswith('?'):
            if cmd.startswith('CAL ') and not self.silent_refusal:
                self.calnum += 1
            self.err |= self.err_on_write
            self.auxerr |= self.auxerr_on_write

    @property
    def commands(self):
        return [c.strip() for c in self.written]


@pytest.fixture
def fake_gpib():
    fake = FakeGpib()
    with patch('caldmm.drivers.hp3458a.Gpib.Gpib', return_value=fake):
        yield fake


def _cal_dev(fake_gpib, **kwargs):
    kwargs.setdefault('allow_calibration_write', True)
    dev = HP3458A(**kwargs)
    fake_gpib.written.clear()
    return dev


class TestAdjustmentKindIsPure:
    """The value envelope is checkable with no instrument attached."""

    @pytest.mark.parametrize('value,kind', [
        (0, 'offset'),
        (0.0, 'offset'),
        (1.0, 'dc_gain'),
        (10.0, 'dc_gain'),
        (10.0001, 'dc_gain'),
        (12.0, 'dc_gain'),
        (1.0e3, 'resistance'),
        (10.0e3, 'resistance'),
        (12.0e3, 'resistance'),
    ])
    def test_documented_bands(self, value, kind):
        assert adjustment_kind(value) == kind

    @pytest.mark.parametrize('value', [0.5, 12.5, 100.0, 999.0, 12001.0,
                                       1.0e6, -10.0])
    def test_undocumented_values_rejected(self, value):
        with pytest.raises(ValueError, match='outside every documented'):
            adjustment_kind(value)

    @pytest.mark.parametrize('value', [float('nan'), float('inf')])
    def test_non_finite_rejected(self, value):
        with pytest.raises(ValueError):
            adjustment_kind(value)

    def test_non_numeric_rejected(self):
        with pytest.raises(ValueError):
            adjustment_kind('ten')

    def test_expected_durations_match_the_manual(self):
        assert expected_adjustment_seconds(0) == 5 * 60
        assert expected_adjustment_seconds(10.0) == 2 * 60
        assert expected_adjustment_seconds(10.0e3) == 12 * 60


class TestCalibrationGating:
    def test_adjust_blocked_without_optin_and_nothing_reaches_the_wire(
            self, fake_gpib):
        dev = HP3458A()
        assert dev.allow_calibration_write is False
        fake_gpib.written.clear()
        with pytest.raises(CalibrationWriteNotAllowed):
            dev.adjust(10.0)
        assert fake_gpib.written == [], 'gate must block before transmission'

    @pytest.mark.parametrize('call', [
        lambda d: d.adjust(10.0),
        lambda d: d.set_cal_string('hi'),
        lambda d: d.set_security_code(3458, 4448),
    ])
    def test_every_write_method_is_gated(self, fake_gpib, call):
        dev = HP3458A()
        fake_gpib.written.clear()
        with pytest.raises(CalibrationWriteNotAllowed):
            call(dev)
        assert fake_gpib.written == []

    def test_optin_flag_is_off_by_default(self, fake_gpib):
        assert HP3458A().allow_calibration_write is False

    def test_read_only_cal_methods_are_not_gated(self, fake_gpib):
        dev = HP3458A()
        assert dev.calnum() == 17
        assert dev.cal_string() == 'CALIBRATED 2026-08-01'

    def test_acal_is_not_gated(self, fake_gpib):
        """ACAL writes no external-adjustment constants, so it stays open."""
        dev = HP3458A()
        fake_gpib.written.clear()
        dev.acal(ACAL.DCV, nowait=True)
        assert fake_gpib.commands == ['ACAL DCV']


class TestAdjustCommandSequence:
    def test_emits_exact_command_sequence_in_order(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        dev.adjust(10.0001, wait=False)
        assert fake_gpib.commands == [
            'ERR?', 'AUXERR?',          # clear stale error state first
            'CALNUM?',                  # count BEFORE
            'CAL 10.0001',              # the irreversible write
            'ERR?',                     # check the outcome
            'CALNUM?',                  # count AFTER — must have advanced
        ]

    def test_security_code_is_appended_when_given(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        dev.adjust(10.0, security_code=SECRET, wait=False)
        assert 'CAL 10,3458' in fake_gpib.commands

    def test_no_comma_when_no_security_code(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        dev.adjust(10.0, wait=False)
        assert 'CAL 10' in fake_gpib.commands

    @pytest.mark.parametrize('value,expected', [
        (0, 'CAL 0'),
        (10.0, 'CAL 10'),
        (10.000042, 'CAL 10.000042'),
        (10000.001, 'CAL 10000.001'),
    ])
    def test_value_formatting(self, fake_gpib, value, expected):
        dev = _cal_dev(fake_gpib)
        dev.adjust(value, wait=False)
        assert expected in fake_gpib.commands

    def test_returns_calnum_after_adjustment(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        before = fake_gpib.calnum
        assert dev.adjust(10.0, wait=False) == before + 1


class TestCalnumIsChecked:
    """A refused CAL cannot advance the counter — the one positive check."""

    def test_stalled_counter_raises_lockout_even_with_clean_registers(
            self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        fake_gpib.silent_refusal = True          # accepts CAL, stores nothing
        with pytest.raises(CalibrationLockoutError) as excinfo:
            dev.adjust(10.0, wait=False)
        message = str(excinfo.value)
        assert 'did not advance' in message
        assert 'J132' in message

    def test_counter_is_read_before_and_after(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        dev.adjust(10.0, wait=False)
        assert fake_gpib.commands.count('CALNUM?') == 2

    def test_stale_output_buffer_is_drained_before_cal(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        fake_gpib.stale_reading = True
        dev.adjust(10.0, wait=False)
        assert 'CAL 10' in fake_gpib.commands

    def test_wait_sleeps_the_documented_duration(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        with patch('caldmm.drivers.hp3458a.time.sleep') as sleep:
            dev.adjust(10.0e3, wait=True)
        assert sleep.call_args.args[0] == 12 * 60

    def test_wait_sec_overrides_the_documented_duration(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        with patch('caldmm.drivers.hp3458a.time.sleep') as sleep:
            dev.adjust(10.0e3, wait=True, wait_sec=1.0)
        assert sleep.call_args.args[0] == 1.0

    def test_wait_sec_larger_than_documented_is_honoured_not_capped(
            self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        with patch('caldmm.drivers.hp3458a.time.sleep') as sleep:
            dev.adjust(10.0, wait=True, wait_sec=9999.0)
        assert sleep.call_args.args[0] == 9999.0

    def test_undocumented_value_rejected_before_transmission(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        with pytest.raises(ValueError):
            dev.adjust(500.0, wait=False)
        assert fake_gpib.written == []

    def test_refuses_while_freerunning_and_sends_nothing(self, fake_gpib):
        dev = _cal_dev(fake_gpib, freerunning=True)
        with pytest.raises(ValueError, match='free-running'):
            dev.adjust(10.0, wait=False)
        assert fake_gpib.written == []


class TestAdjustFailureRaises:
    def test_calibration_error_bit_raises(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        fake_gpib.err_on_write = ERR_CALIBRATION
        with pytest.raises(CalibrationError) as excinfo:
            dev.adjust(10.0, wait=False)
        assert 'calibration error' in str(excinfo.value)

    def test_cal_value_out_of_range_raises_with_actionable_text(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        fake_gpib.err_on_write = ERR_HARDWARE
        fake_gpib.auxerr_on_write = AUXERR_CAL_VALUE_OUT_OF_RANGE
        with pytest.raises(CalibrationError) as excinfo:
            dev.adjust(10.0, wait=False)
        assert 'could not reconcile' in str(excinfo.value)

    def test_auxerr_is_only_read_when_the_hardware_bit_is_set(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        dev.adjust(10.0, wait=False)
        # ERR? twice (pre-clear + post-check), AUXERR? once (pre-clear only).
        assert fake_gpib.commands.count('AUXERR?') == 1

    def test_unexpected_error_bits_still_raise(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        fake_gpib.err_on_write = 2048   # settings conflict
        with pytest.raises(CalibrationError, match='FAILED'):
            dev.adjust(10.0, wait=False)

    def test_clean_registers_do_not_raise(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        dev.adjust(10.0, wait=False)     # no exception


class TestLockoutMessage:
    """The lockout message is the entire user experience of the failure."""

    def _lockout(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        fake_gpib.err_on_write = ERR_HARDWARE
        fake_gpib.auxerr_on_write = AUXERR_CAL_RAM_WRITE_PROTECTION
        with pytest.raises(CalibrationLockoutError) as excinfo:
            dev.adjust(10.0, wait=False)
        return str(excinfo.value)

    def test_names_the_physical_jumper(self, fake_gpib):
        message = self._lockout(fake_gpib)
        assert 'J132' in message

    def test_says_which_assembly_and_that_the_cover_comes_off(self, fake_gpib):
        message = self._lockout(fake_gpib)
        assert '03458-66505' in message
        assert 'top cover' in message

    def test_names_the_security_code_as_the_other_cause(self, fake_gpib):
        message = self._lockout(fake_gpib)
        assert 'SECURE' in message
        assert 'security_code' in message

    def test_says_the_switch_cannot_be_queried_remotely(self, fake_gpib):
        assert 'queried remotely' in self._lockout(fake_gpib)

    def test_says_nothing_was_stored(self, fake_gpib):
        assert 'nothing was stored' in self._lockout(fake_gpib).lower()

    def test_denies_the_rear_panel_switch_a_reader_will_expect(self, fake_gpib):
        """The 3458A has no rear-panel calibration switch; the 5700A does."""
        assert 'no rear-panel calibration switch' in self._lockout(fake_gpib)

    def test_lockout_is_a_calibration_error_subclass(self):
        assert issubclass(CalibrationLockoutError, CalibrationError)

    def test_security_code_absent_from_lockout_message(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        fake_gpib.err_on_write = ERR_HARDWARE
        fake_gpib.auxerr_on_write = AUXERR_CAL_RAM_WRITE_PROTECTION
        with pytest.raises(CalibrationLockoutError) as excinfo:
            dev.adjust(10.0, security_code=987654, wait=False)
        assert '987654' not in str(excinfo.value)


class TestSecurityCodeIsNotDisclosed:
    def test_code_is_not_stored_on_the_instance(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        dev.adjust(10.0, security_code=987654, wait=False)
        assert '987654' not in repr(dev)
        assert all('987654' not in str(v) for v in vars(dev).values())

    def test_set_security_code_does_not_echo_either_code(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        fake_gpib.err_on_write = ERR_CALIBRATION
        with pytest.raises(CalibrationError) as excinfo:
            dev.set_security_code(3458, 4448)
        message = str(excinfo.value)
        assert '3458' not in message
        assert '4448' not in message


class TestSetCalString:
    def test_emits_calstr(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        dev.set_cal_string('CAL 2026-08-01')
        assert "CALSTR 'CAL 2026-08-01'" in fake_gpib.commands

    def test_appends_security_code(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        dev.set_cal_string('hi', security_code=SECRET)
        assert "CALSTR 'hi',3458" in fake_gpib.commands

    def test_overlong_message_rejected_before_transmission(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        with pytest.raises(ValueError, match='at most 75'):
            dev.set_cal_string('x' * 76)
        assert fake_gpib.written == []

    def test_quote_rejected_before_transmission(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        with pytest.raises(ValueError, match='quotes'):
            dev.set_cal_string("it's")
        assert fake_gpib.written == []


class TestSetSecurityCode:
    def test_emits_secure_with_both_codes(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        dev.set_security_code(3458, 4448)
        assert 'SECURE 3458,4448' in fake_gpib.commands

    def test_acal_secure_flag_appended(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        dev.set_security_code(3458, 4448, acal_secure=True)
        assert 'SECURE 3458,4448,ON' in fake_gpib.commands

    def test_string_factory_code_accepted_verbatim(self, fake_gpib):
        """The manuals disagree on whether the factory code is 3458 or 3458A,
        so a valid string must pass through unmangled."""
        dev = _cal_dev(fake_gpib)
        dev.set_security_code('3458A', 0)
        assert 'SECURE 3458A,0' in fake_gpib.commands


class TestSecurityCodeCannotInjectACommand:
    """The code lands on an irreversible write path, so it is validated."""

    @pytest.mark.parametrize('evil', [
        '3458\nCAL 0',          # newline: a second command on the next line
        '3458;CAL 0',           # semicolon: a second command on the same line
        '3458,0',               # comma: an extra argument
        '3458 CAL',             # whitespace
        '3458\rCAL 0',
        '3458\x00',
        '',
        '   ',
    ])
    def test_injection_attempts_rejected_before_transmission(self, fake_gpib, evil):
        dev = _cal_dev(fake_gpib)
        with pytest.raises(ValueError):
            dev.adjust(10.0, security_code=evil, wait=False)
        assert fake_gpib.written == []

    def test_rejection_does_not_echo_the_offending_code(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        with pytest.raises(ValueError) as excinfo:
            dev.adjust(10.0, security_code='3458;CAL 0', wait=False)
        assert '3458' not in str(excinfo.value)

    def test_set_security_code_rejects_injection_too(self, fake_gpib):
        dev = _cal_dev(fake_gpib)
        with pytest.raises(ValueError):
            dev.set_security_code(3458, '0\nCAL 0')
        assert fake_gpib.written == []

    @pytest.mark.parametrize('good', ['3458', '3458A', '-1', '0', 'ABC123'])
    def test_legitimate_codes_still_accepted(self, fake_gpib, good):
        dev = _cal_dev(fake_gpib)
        dev.adjust(10.0, security_code=good, wait=False)
        assert f'CAL 10,{good}' in fake_gpib.commands


class TestAutozeroAndOcompSetters:
    """The B6 carry-over: the cached attributes must stay truthful."""

    def test_set_autozero_sends_command_and_updates_cache(self, fake_gpib):
        dev = HP3458A(azero=True)
        fake_gpib.written.clear()
        dev.set_autozero(False)
        assert fake_gpib.commands == ['AZERO OFF']
        assert dev.autozero is False
        dev.set_autozero(True)
        assert dev.autozero is True
        assert fake_gpib.commands == ['AZERO OFF', 'AZERO ON']

    def test_set_ocomp_sends_command_and_updates_cache(self, fake_gpib):
        dev = HP3458A(ocomp=True)
        fake_gpib.written.clear()
        dev.set_ocomp(False)
        assert fake_gpib.commands == ['OCOMP OFF']
        assert dev.ocomp is False
        dev.set_ocomp(True)
        assert dev.ocomp is True

    def test_property_setters_delegate(self, fake_gpib):
        dev = HP3458A()
        fake_gpib.written.clear()
        dev.autozero = False
        dev.ocomp = False
        assert fake_gpib.commands == ['AZERO OFF', 'OCOMP OFF']
        assert dev.autozero is False
        assert dev.ocomp is False

    def test_constructor_values_are_reflected(self, fake_gpib):
        dev = HP3458A(azero=False, ocomp=False)
        assert dev.autozero is False
        assert dev.ocomp is False
        assert 'AZERO OFF' in fake_gpib.commands
        assert 'OCOMP OFF' in fake_gpib.commands

    def test_raw_cmd_bypasses_the_setters_and_leaves_the_cache_stale(
            self, fake_gpib):
        """Characterises the *driver's* raw cmd() path, which is the mechanism
        cal/adapters.py currently uses. This does not pin adapters.py itself —
        switching adapters.py to the setters would not make this fail."""
        dev = HP3458A(azero=True)
        dev.cmd('AZERO OFF')
        assert dev.autozero is True          # cache now disagrees with the wire
        dev.set_autozero(False)
        assert dev.autozero is False


class TestErrorRegisters:
    def test_error_register_reads_and_returns_int(self, fake_gpib):
        dev = HP3458A()
        fake_gpib.err = 6
        assert dev.error_register() == 6

    def test_error_register_tolerates_exponential_form(self, fake_gpib):
        dev = HP3458A()
        fake_gpib._pending.append(b'+2.00000000E+00\r\n')
        assert dev.error_register() == 2

    def test_aux_error_register_reads_and_returns_int(self, fake_gpib):
        dev = HP3458A()
        fake_gpib.auxerr = 16384
        assert dev.aux_error_register() == 16384

"""Tests for caldmm.apps.fluke5700a_console (task D1).

Offline only — no hardware, no linux-gpib, and nothing that blocks. The
instrument-facing tests drive a real :class:`caldmm.drivers.fluke5700a.Fluke5700A`
over :class:`tests.drivers.fake_gpib.FakeTransport`, so what is asserted is the
**command traffic**, not merely what was printed. When the operator declines a
dangerous action the test asserts the command is absent from ``transport.log``:
"a message was printed" is not evidence that anything was prevented.
"""

from unittest.mock import MagicMock, patch

import pytest

from caldmm.drivers import fluke5700a as f57
from caldmm.apps import fluke5700a_console as fc
from caldmm.apps.fluke5700a_console import (
    ConsoleError,
    NO_ASSEMBLY,
    ServiceConsole,
    StatusSnapshot,
    fault_assemblies,
    format_banner,
    format_esr,
    format_explain,
    format_fault_groups,
    format_faults,
    format_help,
    format_isr,
    format_isr_change,
    format_status,
    format_stb,
    glue_value_tokens,
    group_faults_by_assembly,
    parse_amplitude,
    parse_frequency,
    parse_ohms,
    parse_onoff,
    parse_value,
)
from caldmm.drivers.fluke5700a import (
    Fluke5700A,
    ISR_OPER,
    ISR_REMOTE,
    ISR_SETTLED,
)
from caldmm.lib.console import OperatorConsole

from tests.drivers.fake_gpib import FakeTransport


GOOD_IDN = "FLUKE,5720A,5248000,1.0+B+*"


def make_session(*, isr=ISR_REMOTE, service=False, answers=None, **responses):
    """Return ``(session, transport)`` for an opened calibrator.

    *answers* is a queue consumed by the monkeypatched ``input``; pass it via
    :func:`patch_input` in the test. Extra keyword arguments script additional
    transport responses.
    """
    scripted = {
        "FAULT?": "0",
        "*IDN?": GOOD_IDN,
        "*OPT?": '"5725A Attached"',
        "ISR?": str(isr),
        "ISCR?": "0",
    }
    scripted.update(responses)
    transport = FakeTransport(scripted)
    dev = Fluke5700A(transport=transport, service=service)
    session = ServiceConsole(dev, OperatorConsole(), service=service)
    session.poll_interval = 0.0
    return session, transport


@pytest.fixture
def patch_input(monkeypatch):
    """Feed a queue of answers to every ``input()`` the console makes.

    An answer that is an exception (class or instance) is raised instead of
    returned, which is how the EOF-at-the-prompt cases are driven. Returns
    the list the prompts are recorded into, so a test can assert on the exact
    wording the operator was shown.
    """
    prompts: "list[str]" = []

    def install(*answers):
        queue = list(answers)

        def fake_input(prompt=""):
            prompts.append(prompt)
            if not queue:
                raise AssertionError(
                    f"the console asked for more input than the test "
                    f"scripted (prompt {prompt!r})")
            answer = queue.pop(0)
            if isinstance(answer, type) and issubclass(answer, BaseException):
                raise answer()
            if isinstance(answer, BaseException):
                raise answer
            return answer

        monkeypatch.setattr("builtins.input", fake_input)
        return prompts

    return install


# ── value parsing ─────────────────────────────────────────────────────

class TestParseValue:
    @pytest.mark.parametrize("text,expected", [
        # the brief's table
        ("10V", (10.0, "V")),
        ("100mV", (0.1, "V")),
        ("1.5mA", (1.5e-3, "A")),
        ("10k", (1e4, "")),
        ("1.9M", (1.9e6, "")),
        ("1kHz", (1000.0, "HZ")),
        ("1MHz", (1e6, "HZ")),
        # signs, exponents and zero
        ("-10V", (-10.0, "V")),
        ("+10V", (10.0, "V")),
        ("0", (0.0, "")),
        ("1E-3V", (1e-3, "V")),
        (".5V", (0.5, "V")),
        # unit spellings are case-insensitive, prefixes are not
        ("10v", (10.0, "V")),
        ("10ohm", (10.0, "OHM")),
        ("10ohms", (10.0, "OHM")),
        ("10kohm", (1e4, "OHM")),
        ("10KOHM", (1e4, "OHM")),
        ("1khz", (1000.0, "HZ")),
        # micro in both spellings
        ("10uA", (1e-5, "A")),
        ("10µA", (1e-5, "A")),
        # whitespace is tolerated
        ("  10 V  ", (10.0, "V")),
    ])
    def test_table(self, text, expected):
        value, unit = parse_value(text)
        assert unit == expected[1]
        assert value == pytest.approx(expected[0])

    def test_lowercase_m_is_milli_uppercase_M_is_mega(self):
        # The one rule the whole console rests on, stated as an assertion.
        assert parse_value("1m")[0] == pytest.approx(1e-3)
        assert parse_value("1M")[0] == pytest.approx(1e6)

    @pytest.mark.parametrize("text", ["100MV", "1.5MA", "1MV", "10Ma"])
    def test_mega_on_an_amplitude_is_refused_not_guessed(self, text):
        # The 5700A's own syntax reads a bare M on an amplitude as milli
        # (guide §3.6) while SI reads it as mega. The console refuses rather
        # than picking one silently -- no 5700A range reaches a megavolt.
        with pytest.raises(ConsoleError) as excinfo:
            parse_value(text)
        message = str(excinfo.value)
        assert "mega" in message and "milli" in message

    def test_mega_on_ohms_and_hertz_is_accepted(self):
        assert parse_value("1.9M")[0] == pytest.approx(1.9e6)
        assert parse_value("1.9Mohm") == (pytest.approx(1.9e6), "OHM")
        assert parse_value("1MHz") == (pytest.approx(1e6), "HZ")

    @pytest.mark.parametrize("text,fragment", [
        ("", "empty"),
        ("   ", "empty"),
        ("banana", "does not start with a number"),
        ("V10", "does not start with a number"),
        ("10x", "unknown multiplier"),
        ("10zzz", "unknown suffix"),
        ("10 volts", "unknown suffix"),
    ])
    def test_garbage_is_a_clear_message_not_a_traceback(self, text, fragment):
        with pytest.raises(ConsoleError) as excinfo:
            parse_value(text)
        assert fragment in str(excinfo.value)

    def test_wrong_case_multiplier_suggests_the_right_one(self):
        with pytest.raises(ConsoleError) as excinfo:
            parse_value("10N")
        assert "'n'" in str(excinfo.value)

    def test_none_is_rejected(self):
        with pytest.raises(ConsoleError):
            parse_value(None)


class TestParseHelpers:
    def test_amplitude_requires_a_unit(self):
        with pytest.raises(ConsoleError) as excinfo:
            parse_amplitude("10")
        assert "no unit" in str(excinfo.value)

    def test_amplitude_rejects_a_frequency(self):
        with pytest.raises(ConsoleError) as excinfo:
            parse_amplitude("1kHz")
        assert "frequency" in str(excinfo.value)

    def test_amplitude_accepts_v_a_ohm(self):
        assert parse_amplitude("10V") == (10.0, "V")
        assert parse_amplitude("1.5mA") == (pytest.approx(1.5e-3), "A")
        assert parse_amplitude("10kohm") == (pytest.approx(1e4), "OHM")

    @pytest.mark.parametrize("text,expected", [
        ("1kHz", 1000.0), ("1000", 1000.0), ("1k", 1000.0), ("1MHz", 1e6),
    ])
    def test_frequency(self, text, expected):
        assert parse_frequency(text) == pytest.approx(expected)

    def test_frequency_rejects_volts(self):
        with pytest.raises(ConsoleError):
            parse_frequency("10V")

    @pytest.mark.parametrize("text,expected", [
        ("10k", 1e4), ("1.9M", 1.9e6), ("0", 0.0), ("100", 100.0),
        ("10kohm", 1e4),
    ])
    def test_ohms(self, text, expected):
        assert parse_ohms(text) == pytest.approx(expected)

    def test_ohms_rejects_volts(self):
        with pytest.raises(ConsoleError):
            parse_ohms("10V")

    @pytest.mark.parametrize("text,expected", [
        ("on", True), ("ON", True), ("1", True), ("yes", True),
        ("off", False), ("OFF", False), ("0", False), ("no", False),
    ])
    def test_onoff(self, text, expected):
        assert parse_onoff(text) is expected

    def test_onoff_rejects_anything_else(self):
        with pytest.raises(ConsoleError):
            parse_onoff("maybe")


class TestGlueValueTokens:
    @pytest.mark.parametrize("tokens,expected", [
        (["10", "V"], ["10V"]),
        (["10V"], ["10V"]),
        (["100", "V", "1", "kHz"], ["100V", "1kHz"]),
        (["100V", "1kHz"], ["100V", "1kHz"]),
        (["1.9", "M"], ["1.9M"]),
        # not a value pair: left exactly as it was
        (["CAL", "DC220V"], ["CAL", "DC220V"]),
        (["10", "20"], ["10", "20"]),
        ([], []),
    ])
    def test_table(self, tokens, expected):
        assert glue_value_tokens(tokens) == expected


# ── formatters ────────────────────────────────────────────────────────

class TestRegisterFormatters:
    def test_isr_names_every_bit(self):
        text = format_isr(ISR_REMOTE | ISR_OPER)
        assert "REMOTE" in text and "OPER" in text
        # every bit, set or clear, is named -- not a bare hex value
        for name in ("RPTBUSY", "SETTLED", "WBND", "SCALE", "OFFSET", "PLOCK",
                     "PSHFT", "RLOCK", "RCOMP", "BOOST", "EXSENS", "EXGARD"):
            assert name in text
        assert "set: REMOTE, OPER" in text

    def test_esr_names_every_bit(self):
        text = format_esr(0b00100001)
        assert "set: CME, OPC" in text
        for name in ("PON", "EXE", "DDE", "QYE"):
            assert name in text

    def test_stb_names_every_bit(self):
        text = format_stb(0b01010000)
        assert "set: RQS_MSS, MAV" in text
        for name in ("ESB", "EAV", "ISCB"):
            assert name in text

    def test_no_bits_set_says_so(self):
        assert "set: (none)" in format_esr(0)

    def test_isr_change_marks_the_bits_that_moved(self):
        text = format_isr_change(ISR_REMOTE, ISR_REMOTE | ISR_OPER)
        assert "+OPER" in text
        text = format_isr_change(ISR_REMOTE | ISR_OPER, ISR_REMOTE)
        assert "-OPER" in text

    def test_isr_change_first_sample_lists_set_bits(self):
        assert "REMOTE" in format_isr_change(None, ISR_REMOTE)

    def test_isr_change_with_no_change(self):
        assert "no bit changed" in format_isr_change(ISR_REMOTE, ISR_REMOTE)


class TestFormatOutput:
    @pytest.mark.parametrize("args,expected", [
        ((100.0, "V", 0.0), "100.000000V dc"),
        ((100.0, "V", 1000.0), "100.000000V @ 1.000000kHz"),
        ((1.5e-3, "A", 0.0), "1.500000mA dc"),
        ((1e4, "OHM", 0.0), "10.000000kΩ"),
    ])
    def test_table(self, args, expected):
        assert fc.format_output(*args) == expected


class TestFormatStatus:
    def test_renders_from_canned_values_with_no_instrument(self):
        snap = StatusSnapshot(
            state=(0, "dc volts"),
            isr=ISR_REMOTE | ISR_OPER | ISR_SETTLED,
            esr=0,
            stb=0b00010000,
            out=(100.0, "V", 0.0),
            range_id="DC220V",
            uncert=(6.5, "PPM", 365))
        text = format_status(snap)
        assert "STATE 0  Operating" in text
        assert "dc volts" in text
        assert "DC220V" in text
        assert "365 day interval" in text
        # registers by name, not hex only
        assert "REMOTE" in text and "SETTLED" in text and "MAV" in text
        assert "OPERATE" in text and "LIVE" in text

    def test_standby_is_reported_as_standby(self):
        assert "STANDBY" in format_status(StatusSnapshot(isr=ISR_REMOTE))

    def test_missing_fields_degrade_to_unavailable(self):
        text = format_status(StatusSnapshot(errors=["ISR? failed: boom"]))
        assert "(unavailable)" in text
        assert "ISR? failed: boom" in text

    def test_uncertainty_sentinel_is_explained(self):
        text = format_status(StatusSnapshot(uncert=(None, "PPM", 365)))
        assert "no specification" in text


class TestFaultGrouping:
    FAULTS = [
        (3101, "A14: 8255 Port A Fault"),
        (3117, "A14/A15: HV +DC Current Error Amp Fault"),
        (3111, "A15: HV +DC High Voltage Output Fault"),
        (2200, "Unknown Command"),
        (3135, "Assembly A14 Not Responding"),
    ]

    @pytest.mark.parametrize("text,expected", [
        ("A15: HV +DC High Voltage Output Fault", ("A15",)),
        ("A14: 8255 Port A Fault", ("A14",)),
        ("A14/A15: HV +DC Current Error Amp Fault", ("A14", "A15")),
        ("A14/A15/A16: 2.2A AC Range Mag. Fault", ("A14", "A15", "A16")),
        ("Assembly A14 Not Responding", ("A14",)),
        ("Unknown Command", ()),
        ("Output Tripped To Standby", ()),
        ("", ()),
    ])
    def test_assembly_extraction(self, text, expected):
        assert fault_assemblies(text) == expected

    def test_grouping_puts_a_shared_fault_in_both_groups(self):
        groups = group_faults_by_assembly(self.FAULTS)
        assert [code for code, _ in groups["A14"]] == [3101, 3117, 3135]
        assert [code for code, _ in groups["A15"]] == [3117, 3111]

    def test_the_most_implicated_assembly_leads(self):
        groups = group_faults_by_assembly(self.FAULTS)
        assert list(groups)[0] == "A14"

    def test_a_fault_with_no_assembly_is_kept_and_listed_last(self):
        groups = group_faults_by_assembly(self.FAULTS)
        assert NO_ASSEMBLY in groups
        assert list(groups)[-1] == NO_ASSEMBLY
        assert groups[NO_ASSEMBLY] == [(2200, "Unknown Command")]

    def test_format_fault_groups_shows_codes_and_assemblies(self):
        text = format_fault_groups(self.FAULTS)
        assert "A14" in text and "A15" in text and NO_ASSEMBLY in text
        assert "3101" in text and "2200" in text

    def test_format_faults_empty_queue(self):
        assert "empty" in format_faults([])
        assert "no faults" in format_fault_groups([])

    def test_format_faults_is_code_text_assembly(self):
        text = format_faults([(3111, "A15: HV +DC High Voltage Output Fault")])
        assert "3111" in text
        assert "HV +DC High Voltage Output Fault" in text
        assert "(A15)" in text


class TestFormatExplain:
    def test_shows_both_sources_when_they_disagree(self):
        text = format_explain(
            3111,
            "A15: HV +DC High Voltage Output Fault",
            "A15: HV +DC High Voltage Output FALT")
        assert "A15: HV +DC High Voltage Output Fault" in text
        assert "A15: HV +DC High Voltage Output FALT" in text
        assert "DISAGREE" in text

    def test_no_disagreement_marker_when_they_agree(self):
        text = format_explain(3111, "A15: x", "A15:   x")   # whitespace only
        assert "DISAGREE" not in text

    def test_missing_instrument_answer_is_shown_as_such(self):
        text = format_explain(3111, "A15: x", None)
        assert "no answer" in text
        assert "DISAGREE" not in text

    def test_unknown_code_says_it_is_not_in_the_table(self):
        text = format_explain(9999, None, None)
        assert "not in the offline table" in text


class TestFormatBanner:
    def test_stale_zeros_are_shouted_about(self):
        text = format_banner(None, [], 60, 45, 10, 365)
        assert "45 DAYS OLD" in text
        assert "1303" in text

    def test_fresh_zeros_are_not(self):
        text = format_banner(None, [], 60, 5, 10, 365)
        assert "DAYS OLD" not in text

    def test_cold_instrument_warns_about_warm_up(self):
        assert "WARM-UP" in format_banner(None, [], 3, 5, 10, 365)
        assert "WARM-UP" not in format_banner(None, [], 60, 5, 10, 365)

    def test_calibration_past_its_interval_is_flagged(self):
        assert "past its interval" in format_banner(None, [], 60, 5, 400, 365)

    def test_unavailable_values_do_not_crash(self):
        text = format_banner(None, [], None, None, None, None)
        assert "(unavailable)" in text


def test_help_documents_the_prefix_rule_unambiguously():
    text = format_help()
    assert "m = MILLI" in text and "M = MEGA" in text
    assert "CASE-SENSITIVE" in text
    # and says how the instrument's own syntax differs
    assert "§3.6" in text
    # and that raw bypasses the checks
    assert "raw" in text and "bypass" in text.lower()


# ── the 22 V confirmation ─────────────────────────────────────────────

class TestHighVoltageConfirmation:
    OUT_100V = "1.000000E+02,V,0.000000E+00"

    def test_declining_does_not_write_OPER(self, patch_input):
        session, transport = make_session(**{"OUT?": self.OUT_100V})
        patch_input("no")
        session.execute("out 100V")
        session.execute("oper")
        # The evidence that matters: the command never reached the bus.
        assert "OPER" not in transport.log
        assert any(entry.startswith("OUT ") for entry in transport.log)

    def test_bare_enter_is_not_enough(self, patch_input):
        session, transport = make_session(**{"OUT?": self.OUT_100V})
        patch_input("")
        session.execute("oper")
        assert "OPER" not in transport.log

    def test_the_word_yes_energizes(self, patch_input):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_SETTLED | ISR_OPER,
            **{"OUT?": self.OUT_100V})
        patch_input("yes")
        session.execute("oper")
        assert "OPER" in transport.log

    def test_below_22V_needs_no_confirmation(self, monkeypatch):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_SETTLED | ISR_OPER,
            **{"OUT?": "1.000000E+01,V,0.000000E+00"})

        def refuse(prompt=""):
            raise AssertionError("the console must not prompt below 22 V")

        monkeypatch.setattr("builtins.input", refuse)
        session.execute("oper")
        assert "OPER" in transport.log

    def test_staging_a_high_voltage_warns_but_stays_in_standby(self, capsys):
        session, transport = make_session()
        session.execute("out 100V")
        out = capsys.readouterr().out
        assert "22" in out
        assert "OPER" not in transport.log
        assert "STBY" in transport.log

    def test_exactly_22V_confirms(self, patch_input):
        # The boundary itself: >= 22 V, so 22.000 V must prompt. The constant
        # is compared in three places and none of them may drift to '>'.
        session, transport = make_session(
            **{"OUT?": "2.200000E+01,V,0.000000E+00"})
        patch_input("no")
        session.execute("oper")
        assert "OPER" not in transport.log

    def test_just_below_22V_does_not_confirm(self, monkeypatch):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_SETTLED | ISR_OPER,
            **{"OUT?": "2.199000E+01,V,0.000000E+00"})
        monkeypatch.setattr(
            "builtins.input",
            lambda prompt="": pytest.fail("must not prompt below 22 V"))
        session.execute("oper")
        assert "OPER" in transport.log

    def test_a_declined_confirmation_is_recorded_as_a_failure(self,
                                                             patch_input):
        # Not merely printed: one-shot mode reads last_error to decide the
        # exit status, and a refusal a script cannot see is a refusal a
        # script will drive straight through.
        session, transport = make_session(
            **{"OUT?": "1.000000E+02,V,0.000000E+00"})
        patch_input("no")
        session.execute("oper")
        assert session.last_error is not None


# ── dBm and current must not bypass the confirmation ──────────────────

class TestDbmAndCurrentConfirmation:
    # 42 dBm re 600 ohm on the standard ac output is ~97 V rms — a dBm
    # setpoint (reachable via ``raw OUT 42 DBM, 442HZ``) is NOT inherently
    # low-voltage, so the gate must resolve it through VOUT?.
    OUT_DBM = "4.200000E+01,DBM,4.420000E+02"
    VOUT_HOT = "9.750000E+01,V,4.420000E+02"
    VOUT_COLD = "2.4494897E+00,V,1.000000E+04"

    def test_dbm_resolving_above_22V_confirms(self, patch_input):
        session, transport = make_session(
            **{"OUT?": self.OUT_DBM, "VOUT?": self.VOUT_HOT})
        patch_input("no")
        session.execute("oper")
        assert "OPER" not in transport.log

    def test_dbm_resolving_below_22V_does_not_confirm(self, monkeypatch):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_SETTLED | ISR_OPER,
            **{"OUT?": self.OUT_DBM, "VOUT?": self.VOUT_COLD})
        monkeypatch.setattr(
            "builtins.input",
            lambda prompt="": pytest.fail("must not prompt below 22 V"))
        session.execute("oper")
        assert "OPER" in transport.log

    def test_dbm_that_vout_cannot_resolve_confirms(self, patch_input):
        # VOUT? unscripted: the fake times out. Fail safe — a dBm output
        # that cannot be proven below 22 V is treated as above it.
        session, transport = make_session(**{"OUT?": self.OUT_DBM})
        patch_input("no")
        session.execute("oper")
        assert "OPER" not in transport.log

    def test_two_amps_confirms(self, patch_input):
        session, transport = make_session(
            **{"OUT?": "2.000000E+00,A,0.000000E+00"})
        patch_input("no")
        session.execute("oper")
        assert "OPER" not in transport.log

    def test_exactly_one_amp_confirms(self, patch_input):
        # The boundary is >=, same as the 22 V gate.
        session, transport = make_session(
            **{"OUT?": "1.000000E+00,A,0.000000E+00"})
        patch_input("no")
        session.execute("oper")
        assert "OPER" not in transport.log

    def test_milliamps_do_not_confirm(self, monkeypatch):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_SETTLED | ISR_OPER,
            **{"OUT?": "1.000000E-02,A,0.000000E+00"})
        monkeypatch.setattr(
            "builtins.input",
            lambda prompt="": pytest.fail("must not prompt below 1 A"))
        session.execute("oper")
        assert "OPER" in transport.log

    def test_staging_a_high_current_warns(self, capsys):
        session, transport = make_session()
        session.execute("out 2A")
        out = capsys.readouterr().out
        assert "1 A" in out
        assert "OPER" not in transport.log


# ── the live-output interlock on long-term commands ───────────────────

class TestLongTermInterlock:
    LIVE_HV = {"OUT?": "1.000000E+02,V,0.000000E+00"}

    def _live_session(self, **extra):
        responses = dict(self.LIVE_HV)
        responses.update(extra)
        return make_session(isr=ISR_REMOTE | ISR_OPER, **responses)

    @pytest.mark.parametrize("command,forbidden", [
        ("test", "*TST?"),
        ("diag D5700", "DIAG D5700;*OPC"),
        ("diagstep D5700", "DIAG D5700;*OPC"),
        ("calzero", "CAL_ZERO;*OPC"),
        ("calchk", "CAL_CHK;*OPC"),
    ])
    def test_refused_with_22V_or_more_live(self, command, forbidden, capsys):
        session, transport = self._live_session()
        session.execute(command)
        assert forbidden not in transport.log
        # and nothing was even armed
        assert not any("*OPC" in entry for entry in transport.log)
        out = capsys.readouterr().out
        assert "refusing" in out
        assert "stby" in out

    def test_diagstep_refusal_does_not_touch_the_nvram_diagflt(self):
        session, transport = self._live_session()
        session.execute("diagstep")
        assert not any(entry.startswith("DIAGFLT") for entry in transport.log)

    def test_allowed_when_the_output_is_in_standby(self, patch_input):
        session, transport = make_session(**{"*TST?": "0"})
        session.execute("test")
        assert "*TST?" in transport.log

    def test_allowed_when_live_but_below_22V(self):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "1.000000E+01,V,0.000000E+00", "*TST?": "0"})
        session.execute("test")
        assert "*TST?" in transport.log

    def test_refused_at_exactly_22V(self, capsys):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "2.200000E+01,V,0.000000E+00", "*TST?": "0"})
        session.execute("test")
        assert "*TST?" not in transport.log
        assert "refusing" in capsys.readouterr().out

    def test_allowed_just_below_22V(self):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "2.199000E+01,V,0.000000E+00", "*TST?": "0"})
        session.execute("test")
        assert "*TST?" in transport.log

    def test_a_refusal_is_recorded_as_a_failure(self):
        session, transport = self._live_session(**{"*TST?": "0"})
        session.execute("test")
        assert session.last_error is not None

    def test_refused_with_two_amps_live(self, capsys):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "2.000000E+00,A,0.000000E+00", "*TST?": "0"})
        session.execute("test")
        assert "*TST?" not in transport.log
        assert "refusing" in capsys.readouterr().out

    def test_allowed_with_milliamps_live(self):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "1.000000E-02,A,0.000000E+00", "*TST?": "0"})
        session.execute("test")
        assert "*TST?" in transport.log

    def test_refused_when_a_dbm_output_resolves_hot(self, capsys):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": TestDbmAndCurrentConfirmation.OUT_DBM,
               "VOUT?": TestDbmAndCurrentConfirmation.VOUT_HOT,
               "*TST?": "0"})
        session.execute("test")
        assert "*TST?" not in transport.log
        assert "refusing" in capsys.readouterr().out

    def test_refused_when_a_dbm_output_cannot_be_resolved(self, capsys):
        # VOUT? unscripted -> times out; a long-term command must not start
        # against an output the console cannot prove is safe.
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": TestDbmAndCurrentConfirmation.OUT_DBM, "*TST?": "0"})
        session.execute("test")
        assert "*TST?" not in transport.log
        assert "refusing" in capsys.readouterr().out

    def test_warns_that_there_is_no_remote_abort(self, capsys):
        session, transport = make_session(**{"*TST?": "0"})
        session.execute("test")
        out = capsys.readouterr().out
        assert "NO REMOTE ABORT" in out
        assert "front-panel STBY" in out


class TestSelfTest:
    def test_true_means_passed(self, capsys):
        session, transport = make_session(**{"*TST?": "0"})
        session.execute("test")
        out = capsys.readouterr().out
        assert "PASSED" in out and "FAILED" not in out

    def test_one_means_failed_and_the_queue_is_reported(self, capsys):
        session, transport = make_session(
            **{"*TST?": "1", "FAULT?": ["0", "4301", "0"],
               "EXPLAIN? 4301": "ROM Checksum"})
        session.execute("test")
        out = capsys.readouterr().out
        assert "FAILED" in out
        assert "4301" in out and "ROM Checksum" in out

    def test_duration_is_measured_and_reported(self, capsys):
        session, transport = make_session(**{"*TST?": "0"})
        session.execute("test")
        assert " s" in capsys.readouterr().out


# ── calzero / calchk confirmation ─────────────────────────────────────

class TestLongTermConfirmation:
    def test_declining_calzero_sends_nothing(self, patch_input, capsys):
        session, transport = make_session()
        patch_input("no")
        session.execute("calzero")
        assert not any("CAL_ZERO" in entry for entry in transport.log)
        assert "declined" in capsys.readouterr().out

    def test_accepting_calzero_runs_it(self, patch_input):
        session, transport = make_session(
            **{"*ESR?": ["0", "1"], "STATE?": "4,dc zeros"})
        patch_input("yes")
        session.execute("calzero")
        assert "CAL_ZERO;*OPC" in transport.log


# ── diagnostics ───────────────────────────────────────────────────────

def diag_session(*, halted_states, faults, isr=ISR_REMOTE, esr="0"):
    """A session whose DIAG run ends halted or complete, as scripted."""
    return make_session(isr=isr, **{
        "DIAGFLT?": "ABORT",
        "*ESR?": esr,
        "STATE?": halted_states,
        "FAULT?": faults,
    })


class TestDiag:
    def test_diag_reports_faults_grouped_by_assembly(self, capsys):
        session, transport = make_session(**{
            "*ESR?": ["0", "1"],
            "STATE?": "1,high voltage",
            "FAULT?": ["0", "3101", "3117", "0"],
        })
        session.execute("diag D5700")
        out = capsys.readouterr().out
        assert "DIAG D5700;*OPC" in transport.log
        assert "3101" in out and "3117" in out
        assert "A14" in out and "A15" in out

    def test_unknown_target_is_a_message_not_a_traceback(self, capsys):
        session, transport = make_session()
        session.execute("diag NOSUCH")
        assert "unknown DIAG target" in capsys.readouterr().out
        assert not any("DIAG" in entry for entry in transport.log)


class TestDiagStep:
    #: STATE? answers: running, then halted at a fault.
    HALTED = ["1,high voltage", "2,halted at 3101"]

    def test_restores_the_previous_diagflt_on_the_normal_path(self):
        # FAULT? queue: open(), the DIAGFLT HALT check, the post-run drain
        # (one fault then empty), the DIAGFLT restore check.
        session, transport = diag_session(
            halted_states="0,idle", faults=["0", "0", "3101", "0", "0"],
            esr=["0", "1"])
        session.execute("diagstep D5700")
        assert "DIAGFLT HALT" in transport.log
        assert "DIAGFLT ABORT" in transport.log
        assert (transport.log.index("DIAGFLT HALT")
                < transport.log.index("DIAGFLT ABORT"))

    def test_restores_the_previous_diagflt_when_the_operator_aborts(
            self, patch_input):
        session, transport = diag_session(
            halted_states=self.HALTED,
            faults=["0", "0", "3101", "0", "0", "0"])
        patch_input("n")
        session.execute("diagstep D5700")
        assert "DIAG ABORT" in transport.log
        assert "DIAGFLT ABORT" in transport.log
        assert (transport.log.index("DIAGFLT HALT")
                < transport.log.index("DIAGFLT ABORT"))

    def test_stepping_sends_DIAG_CONT(self, patch_input, capsys):
        session, transport = make_session(**{
            "DIAGFLT?": "ABORT",
            "*ESR?": "0",
            # halt, then halt again after CONT, then the operator stops
            "STATE?": ["1,run", "2,halted", "2,halted", "2,halted"],
            "FAULT?": ["0", "0", "3101", "0", "3117", "0", "0", "0"],
        })
        patch_input("y", "n")
        session.execute("diagstep D5700")
        assert "DIAG CONT;*OPC" in transport.log
        assert "DIAG ABORT" in transport.log
        out = capsys.readouterr().out
        assert "3101" in out and "3117" in out
        assert "summary" in out
        assert "ABORTED by the operator" in out

    def test_summary_groups_every_fault_seen(self, patch_input, capsys):
        session, transport = diag_session(
            halted_states=self.HALTED,
            faults=["0", "0", "3101", "3117", "0", "0", "0"])
        patch_input("n")
        session.execute("diagstep")
        out = capsys.readouterr().out
        assert "A14" in out and "A15" in out
        assert "3101" in out and "3117" in out

    def test_declined_diagstep_still_reports_what_it_saw(self, patch_input,
                                                        capsys):
        session, transport = diag_session(
            halted_states=self.HALTED,
            faults=["0", "0", "3101", "0", "0", "0"])
        patch_input("n")
        session.execute("diagstep")
        assert "3101" in capsys.readouterr().out

    def test_the_halt_prompt_does_not_offer_r_equals_repeat(self,
                                                            patch_input):
        # The body text says "anything else → DIAG ABORT", but the inline
        # hint is the last thing read before typing -- and 'r' aborts the
        # diagnostic, so offering "r=repeat" there means the opposite of
        # what it says.
        session, transport = diag_session(
            halted_states=self.HALTED,
            faults=["0", "0", "3101", "0", "0", "0"])
        prompts = patch_input("n")
        session.execute("diagstep")
        halt_prompt = prompts[-1]
        assert "r=repeat" not in halt_prompt
        assert "anything else = abort" in halt_prompt

    def test_the_normal_path_says_so_in_the_summary(self, capsys):
        session, transport = diag_session(
            halted_states="0,idle", faults=["0", "0", "3101", "0", "0"],
            esr=["0", "1"])
        session.execute("diagstep D5700")
        assert "ran to the end" in capsys.readouterr().out


class TestDiagStepInterrupted:
    """A run that ends by exception must release the calibrator and say so.

    The diagnostic is *waiting* inside the instrument when it halts. Leaving
    it there is bad; recording "ran to the end" in the operator's triage log
    for a run that was interrupted is worse, because that log is the whole
    output of the workflow this tool exists for.
    """

    HALTED = ["1,high voltage", "2,halted at 3101"]
    FAULTS = ["0", "0", "3101", "0", "0", "0"]

    def test_eof_at_the_halt_prompt_sends_DIAG_ABORT(self, patch_input):
        session, transport = diag_session(halted_states=self.HALTED,
                                          faults=self.FAULTS)
        patch_input(EOFError)
        session.execute("diagstep D5700")
        assert "DIAG ABORT" in transport.log

    def test_eof_at_the_halt_prompt_still_restores_diagflt(self,
                                                           patch_input):
        session, transport = diag_session(halted_states=self.HALTED,
                                          faults=self.FAULTS)
        patch_input(EOFError)
        session.execute("diagstep D5700")
        assert (transport.log.index("DIAGFLT HALT")
                < transport.log.index("DIAGFLT ABORT"))

    def test_the_summary_says_interrupted_not_ran_to_the_end(
            self, patch_input, capsys):
        session, transport = diag_session(halted_states=self.HALTED,
                                          faults=self.FAULTS)
        patch_input(EOFError)
        session.execute("diagstep D5700")
        out = capsys.readouterr().out
        assert "INTERRUPTED" in out
        assert "ran to the end" not in out
        assert "released with DIAG ABORT" in out
        # and what it did find is still reported
        assert "3101" in out

    def test_a_driver_failure_mid_loop_releases_the_halt(self, patch_input,
                                                         capsys):
        # The likely real trigger on the faulty unit: DIAG CONT times out
        # while the calibrator sits halted at a fault.
        session, transport = diag_session(halted_states=self.HALTED,
                                          faults=self.FAULTS)

        def boom(*args, **kwargs):
            raise TimeoutError("DIAG CONT did not complete within 1800 s")

        session.dev.diag_cont = boom
        patch_input("y")
        session.execute("diagstep D5700")
        out = capsys.readouterr().out
        assert "DIAG ABORT" in transport.log
        assert "INTERRUPTED (TimeoutError)" in out
        assert "ran to the end" not in out
        assert session.last_error is not None

    def test_a_failed_release_warns_that_the_unit_is_still_halted(
            self, patch_input, capsys):
        session, transport = diag_session(halted_states=self.HALTED,
                                          faults=self.FAULTS)

        def boom(*args, **kwargs):
            raise TimeoutError("DIAG CONT timed out")

        def abort_fails():
            raise TimeoutError("DIAG ABORT timed out too")

        session.dev.diag_cont = boom
        session.dev.diag_abort = abort_fails
        patch_input("y")
        session.execute("diagstep D5700")
        out = capsys.readouterr().out
        assert "STILL HALTED" in out
        assert "front panel" in out

    def test_the_fault_list_is_trimmed_with_a_visible_label(self,
                                                            patch_input,
                                                            capsys):
        # Five faults, four shown: the operator must not read the list as
        # complete.
        session, transport = make_session(**{
            "DIAGFLT?": "ABORT",
            "*ESR?": "0",
            "STATE?": ["1,run", "2,halted"],
            "FAULT?": ["0", "0", "3101", "3117", "3122", "3128", "3135",
                       "0", "0", "0"],
        })
        patch_input("n")
        session.execute("diagstep D5700")
        assert "last 4 of 5" in capsys.readouterr().out


class TestDiagflt:
    def test_query_reports_the_mode(self, capsys):
        session, transport = make_session(**{"DIAGFLT?": "ABORT"})
        session.execute("diagflt")
        assert "ABORT" in capsys.readouterr().out

    def test_setting_it_warns_about_nvram(self, capsys):
        session, transport = make_session()
        session.execute("diagflt HALT")
        out = capsys.readouterr().out
        assert "DIAGFLT HALT" in transport.log
        assert "NONVOLATILE" in out

    def test_unknown_mode_is_a_message(self, capsys):
        session, transport = make_session()
        session.execute("diagflt SOMETIMES")
        assert "unknown DIAGFLT mode" in capsys.readouterr().out
        assert not any(entry.startswith("DIAGFLT ") for entry in transport.log)


# ── explain, faults, status against the fake instrument ───────────────

class TestExplainCommand:
    def test_shows_both_sources_when_they_differ(self, capsys):
        session, transport = make_session(
            **{"EXPLAIN? 3111": '"A15: HV +DC High Voltage Output FALT"'})
        session.execute("explain 3111")
        out = capsys.readouterr().out
        assert "DISAGREE" in out
        assert "Output Fault" in out          # the offline table's spelling
        assert "Output FALT" in out           # the instrument's

    def test_falls_back_to_the_offline_table_when_EXPLAIN_dies(self, capsys):
        session, transport = make_session()   # EXPLAIN? unscripted -> timeout
        session.execute("explain 3111")
        out = capsys.readouterr().out
        assert "HV +DC High Voltage Output Fault" in out
        assert "no answer" in out

    def test_bad_argument_is_a_message(self, capsys):
        session, transport = make_session()
        session.execute("explain three")
        assert "not a fault code" in capsys.readouterr().out

    def test_missing_argument_is_a_usage_message(self, capsys):
        session, transport = make_session()
        session.execute("explain")
        assert "usage" in capsys.readouterr().out


class TestFaultsCommand:
    def test_drains_and_groups(self, capsys):
        session, transport = make_session(
            **{"FAULT?": ["0", "3101", "3117", "0"]})
        session.execute("faults")
        out = capsys.readouterr().out
        assert "3101" in out and "3117" in out
        assert "A14" in out

    def test_empty_queue(self, capsys):
        session, transport = make_session()
        session.execute("faults")
        assert "empty" in capsys.readouterr().out


class TestStatusCommand:
    def test_reads_and_renders(self, capsys):
        session, transport = make_session(**{
            "STATE?": "0,dc volts",
            "OUT?": "1.000000E+01,V,0.000000E+00",
            "RANGE?": "DC22V",
            "UNCERT?": "6.5,PPM,365",
            "*ESR?": "0",
        })
        session.execute("status")
        out = capsys.readouterr().out
        assert "DC22V" in out and "REMOTE" in out and "Operating" in out

    def test_a_dead_query_does_not_cost_the_whole_screen(self, capsys):
        session, transport = make_session(**{"STATE?": "0,dc volts"})
        session.execute("status")            # RANGE?/UNCERT?/OUT? unscripted
        out = capsys.readouterr().out
        assert "Operating" in out
        assert "failed" in out


# ── sourcing ──────────────────────────────────────────────────────────

class TestSourcing:
    def test_out_sends_plain_base_units_never_a_prefix(self, transport=None):
        session, transport = make_session()
        session.execute("out 100mV")
        sent = [entry for entry in transport.log if entry.startswith("OUT ")]
        assert sent == ["OUT 1.0000000000E-01 V, 0.000000E+00 HZ"]

    def test_out_with_a_frequency_sends_one_command(self):
        session, transport = make_session()
        session.execute("out 1V 1kHz")
        sent = [entry for entry in transport.log if entry.startswith("OUT ")]
        assert sent == ["OUT 1.0000000000E+00 V, 1.000000E+03 HZ"]

    def test_ohms_never_carries_a_frequency(self):
        session, transport = make_session()
        session.execute("ohms 10k")
        sent = [entry for entry in transport.log if entry.startswith("OUT ")]
        assert sent == ["OUT 1.0000000000E+04 OHM"]

    def test_out_of_envelope_sources_nothing_and_lists_every_violation(
            self, capsys):
        session, transport = make_session()

        def must_not_be_called(*args, **kwargs):
            raise AssertionError(
                "the console's own validate_output check must refuse before "
                "the driver is touched")

        # Without this stub the test passes even if the console-side check is
        # deleted, because set_output validates too and raises a ValueError
        # whose message also begins "refusing to source". The stub, and the
        # "nothing was sent" wording, are what make this test about the
        # console.
        session.dev.set_output = must_not_be_called
        session.execute("out 2000V")
        out = capsys.readouterr().out
        assert "nothing was sent" in out
        assert "exceeds the 1100 V dc-voltage maximum" in out   # the violation
        assert not any(entry.startswith("OUT ") for entry in transport.log)

    def test_a_frequency_with_ohms_is_refused_offline(self, capsys):
        session, transport = make_session()
        session.execute("out 10kohm 1kHz")
        assert "812" in capsys.readouterr().out
        assert not any(entry.startswith("OUT ") for entry in transport.log)

    def test_a_frequency_with_the_ohms_command_is_refused_too(self, capsys):
        # `out 10kohm 1kHz` already refuses; `ohms 10k 1kHz` used to stage
        # 10 kOhm and drop the frequency without a word, which is the same
        # operator mistake answered two different ways.
        session, transport = make_session()
        session.execute("ohms 10k 1kHz")
        out = capsys.readouterr().out
        assert "812" in out
        assert "usage: ohms <value>" in out
        assert not any(entry.startswith("OUT ") for entry in transport.log)

    def test_spaces_between_value_and_unit_are_forgiven(self):
        session, transport = make_session()
        session.execute("out 10 V")
        assert "OUT 1.0000000000E+01 V, 0.000000E+00 HZ" in transport.log

    def test_too_many_arguments_is_a_usage_message(self, capsys):
        session, transport = make_session()
        session.execute("out 10V 1kHz 3")
        assert "usage" in capsys.readouterr().out

    def test_modifiers(self):
        session, transport = make_session()
        session.execute("rangelck on")
        session.execute("extsense off")
        session.execute("extguard on")
        assert "RANGELCK ON" in transport.log
        assert "EXTSENSE OFF" in transport.log
        assert "EXTGUARD ON" in transport.log

    def test_modifier_without_an_argument_is_a_usage_message(self, capsys):
        session, transport = make_session()
        session.execute("rangelck")
        assert "usage" in capsys.readouterr().out


# ── raw ───────────────────────────────────────────────────────────────

class TestRaw:
    def test_a_query_is_read_back(self, capsys):
        session, transport = make_session(**{"RANGE?": "DC22V"})
        session.execute("raw RANGE?")
        assert "DC22V" in capsys.readouterr().out

    def test_a_write_is_sent_verbatim(self):
        session, transport = make_session()
        session.execute("raw DIAG ABORT")
        assert "DIAG ABORT" in transport.log

    def test_faults_are_drained_and_reported_afterwards(self, capsys):
        session, transport = make_session(**{"FAULT?": ["0", "2200", "0"]})
        session.execute("raw WIBBLE")
        out = capsys.readouterr().out
        assert "2200" in out and "Unknown Command" in out

    def test_a_dangerous_write_is_gated_behind_service(self, capsys):
        session, transport = make_session()
        session.execute("raw CAL_STORE")
        assert "CAL_STORE" not in transport.log
        assert "--service" in capsys.readouterr().out

    def test_service_mode_lets_it_through(self):
        session, transport = make_session(service=True)
        session.execute("raw CAL_STORE")
        assert "CAL_STORE" in transport.log

    @pytest.mark.parametrize("line,forbidden", [
        ("raw CAL_STORE ?", "CAL_STORE ?"),
        ("raw OPER ?", "OPER ?"),
        ("raw CAL_ZERO ?", "CAL_ZERO ?"),
    ])
    def test_a_trailing_question_mark_does_not_waive_the_gate(
            self, line, forbidden, capsys):
        # The gate keys on the head token. A '?' on the end makes it look
        # like a query but 'CAL_STORE ?' is still the command that burns
        # EEPROM.
        session, transport = make_session()
        session.execute(line)
        assert forbidden not in transport.log
        assert not any(entry.startswith("CAL_") or entry.startswith("OPER")
                       for entry in transport.log)
        assert "--service" in capsys.readouterr().out

    @pytest.mark.parametrize("line,expected", [
        ("raw ETIME?", "ETIME?"),
        ("raw CAL_DAYS? CAL", "CAL_DAYS? CAL"),
    ])
    def test_a_real_query_whose_stem_is_gated_still_works(self, line,
                                                          expected):
        # 'ETIME?' is a different head token from the gated 'ETIME', and it
        # only reads.
        session, transport = make_session(**{"ETIME?": "128940",
                                             "CAL_DAYS? CAL": "512"})
        session.execute(line)
        assert expected in transport.log

    def test_an_unanswered_query_explains_gap_G31(self, capsys):
        session, transport = make_session()
        session.execute("raw NOSUCH?")
        assert "no answer" in capsys.readouterr().out

    def test_missing_argument_is_a_usage_message(self, capsys):
        session, transport = make_session()
        session.execute("raw")
        assert "usage" in capsys.readouterr().out


# ── dispatch ──────────────────────────────────────────────────────────

class TestDispatch:
    def test_unknown_command_is_a_helpful_message(self, capsys):
        session, transport = make_session()
        assert session.execute("frobnicate") is True
        out = capsys.readouterr().out
        assert "unknown command" in out and "help" in out

    def test_blank_and_comment_lines_are_ignored(self, capsys):
        session, transport = make_session()
        assert session.execute("") is True
        assert session.execute("   ") is True
        assert session.execute("# a note") is True
        assert capsys.readouterr().out == ""

    @pytest.mark.parametrize("word", ["quit", "exit", "q"])
    def test_quit_words_end_the_session(self, word):
        session, transport = make_session()
        assert session.execute(word) is False

    def test_a_driver_error_is_reported_not_raised(self, capsys):
        session, transport = make_session()
        session.execute("range")             # RANGE? unscripted -> timeout
        out = capsys.readouterr().out
        assert "!" in out
        assert "Error" in out or "error" in out

    def test_help_lists_every_dispatchable_command(self):
        session, transport = make_session()
        documented = {name for name, _, _ in fc.COMMAND_HELP if name}
        dispatchable = set(session._handlers) | {"quit"}
        assert documented == dispatchable

    def test_malformed_arguments_never_raise(self, capsys):
        session, transport = make_session()
        for line in ("out banana", "ohms 10V", "explain zzz", "watch nope",
                     "rcomp perhaps", "calshift CAL", "calconst",
                     "diag D5700 extra"):
            assert session.execute(line) is True
        assert "Traceback" not in capsys.readouterr().out


# ── watch ─────────────────────────────────────────────────────────────

class TestWatch:
    # ``transport.script`` resets the consumed-index, so these queues start
    # fresh *after* open() has had its own ISR? answer.
    def test_prints_only_on_change(self, capsys):
        session, transport = make_session()
        transport.script("ISR?", [str(ISR_REMOTE)] * 3)
        session.cmd_watch(["0"], max_polls=3)
        out = capsys.readouterr().out
        assert out.count("ISR 0x") == 1      # first sample only

    def test_a_changed_bit_is_obvious(self, capsys):
        session, transport = make_session()
        transport.script("ISR?", [str(ISR_REMOTE), str(ISR_REMOTE | ISR_OPER)])
        session.cmd_watch(["0"], max_polls=2)
        assert "+OPER" in capsys.readouterr().out

    def test_new_faults_are_printed_with_their_assembly(self, capsys):
        session, transport = make_session()
        transport.script("ISR?", [str(ISR_REMOTE)] * 2)
        transport.script("FAULT?", ["0", "3101", "0"])
        session.cmd_watch(["0"], max_polls=2)
        out = capsys.readouterr().out
        assert "3101" in out and "A14" in out

    def test_negative_interval_is_refused(self):
        session, transport = make_session()
        with pytest.raises(ConsoleError):
            session.cmd_watch(["-1"])


# ── service gating ────────────────────────────────────────────────────

class TestServiceGating:
    def test_fatality_needs_service(self, capsys):
        session, transport = make_session()
        session.execute("fatality")
        assert "--service" in capsys.readouterr().out
        assert "FATALITY?" not in transport.log

    def test_fatalclr_needs_service_and_a_typed_yes(self, patch_input,
                                                   capsys):
        session, transport = make_session(service=True)
        patch_input("no")
        session.execute("fatalclr")
        assert "FATALCLR" not in transport.log
        assert "declined" in capsys.readouterr().out


# ── housekeeping commands ─────────────────────────────────────────────

class TestHousekeeping:
    def test_caldays_warns_about_stale_zeros(self, capsys):
        session, transport = make_session(
            **{"CAL_DAYS? CAL": "10", "CAL_DAYS? ZERO": "45"})
        session.execute("caldays")
        out = capsys.readouterr().out
        assert "45 days" in out and "1303" in out

    def test_ontime_warns_when_cold(self, capsys):
        session, transport = make_session(**{"ONTIME?": "3"})
        session.execute("ontime")
        assert "warmed up" in capsys.readouterr().out

    def test_lrn_warns_that_replay_can_energize(self, capsys):
        session, transport = make_session(**{"*LRN?": "OUT 10V;OPER"})
        session.execute("lrn")
        assert "ENERGIZE" in capsys.readouterr().out

    def test_echo_flags_a_mismatch(self, capsys):
        session, transport = make_session(**{'ECHO? "ping"': '"pong"'})
        session.execute("echo ping")
        assert "did not echo it verbatim" in capsys.readouterr().out

    def test_idn_and_opt(self, capsys):
        session, transport = make_session()
        session.execute("idn")
        session.execute("opt")
        out = capsys.readouterr().out
        assert "5720A" in out and "5725A Attached" in out


# ── the banner and one-shot mode ──────────────────────────────────────

def one_shot_args(monkeypatch, argv):
    """Parse *argv*, with the global SIGINT handler install stubbed out."""
    monkeypatch.setattr(fc, "setup_graceful_shutdown", lambda cleanup: None)
    return fc.build_parser().parse_args(argv)


class TestTerminationSignals:
    """M12: SIGTERM and SIGHUP must reach the STBY cleanup.

    Only SIGINT ran it.  SIGTERM's default action terminates the interpreter
    without unwinding, so an ssh drop, a ``timeout`` or a
    systemd stop left the output in whatever state it was -- including
    OPERATE, at up to 1100 V ([OM Safety Summary]).
    """

    def test_both_are_installed(self, monkeypatch):
        import signal
        installed = {}
        monkeypatch.setattr(fc, "setup_graceful_shutdown", lambda cleanup: None)
        monkeypatch.setattr(signal, "signal",
                            lambda sig, handler: installed.setdefault(sig,
                                                                      handler))
        session, transport = make_session(**{"FAULT?": "0"})
        args = fc.build_parser().parse_args(["--no-banner", "--faults"])
        fc.run(args, dev=session.dev)
        assert signal.SIGTERM in installed
        assert signal.SIGHUP in installed

    def test_the_handler_unwinds_so_the_finally_runs(self, monkeypatch):
        import signal
        installed = {}
        monkeypatch.setattr(fc, "setup_graceful_shutdown", lambda cleanup: None)
        monkeypatch.setattr(signal, "signal",
                            lambda sig, handler: installed.setdefault(sig,
                                                                      handler))
        session, transport = make_session(**{"FAULT?": "0"})
        args = fc.build_parser().parse_args(["--no-banner", "--faults"])
        fc.run(args, dev=session.dev)
        with pytest.raises(SystemExit):
            installed[signal.SIGTERM](signal.SIGTERM, None)

    def test_the_previous_dispositions_are_restored(self, monkeypatch):
        """The handler must not outlive the session it was installed for.

        Left in place it raises SystemExit inside whatever runs next -- a
        test runner, an interactive shell -- on a signal that has nothing to
        do with this calibrator.
        """
        import signal
        before = {sig: signal.getsignal(sig)
                  for sig in fc.TERMINATION_SIGNALS}
        session, transport = make_session(**{"FAULT?": "0"})
        args = one_shot_args(monkeypatch, ["--no-banner", "--faults"])
        fc.run(args, dev=session.dev)
        assert {sig: signal.getsignal(sig)
                for sig in fc.TERMINATION_SIGNALS} == before

    def test_the_cleanup_drops_the_output(self, monkeypatch):
        """What the handler is for: STBY, whichever way the process ends."""
        session, transport = make_session(**{"FAULT?": "0"})
        args = one_shot_args(monkeypatch, ["--no-banner", "--faults"])
        fc.run(args, dev=session.dev)
        assert "STBY" in transport.log

    def test_a_standby_that_would_not_confirm_is_printed_not_swallowed(
            self, monkeypatch, capsys):
        """The driver verifies STBY (``ISR?`` bit 0) and raises when it did
        not take.  This cleanup used to swallow that, which turns "the output
        may still be live" into a clean exit."""
        session, transport = make_session(**{"FAULT?": "0"})

        def refuse():
            raise RuntimeError("STBY sent but the OPER bit is still set")

        monkeypatch.setattr(session.dev, "standby", refuse)
        args = one_shot_args(monkeypatch, ["--no-banner", "--faults"])
        fc.run(args, dev=session.dev)
        err = capsys.readouterr().err
        assert "STANDBY VERIFICATION FAILED" in err
        assert "may still be live" in err
        assert "OPER bit is still set" in err


class TestOneShot:
    def _args(self, monkeypatch, argv):
        return one_shot_args(monkeypatch, argv)

    def test_status_runs_and_exits_cleanly(self, monkeypatch, capsys):
        session, transport = make_session(**{
            "STATE?": "0,dc volts", "OUT?": "0.0,V,0.0", "RANGE?": "DC22V",
            "UNCERT?": "6.5,PPM,365", "*ESR?": "0", "ONTIME?": "60",
            "CAL_DAYS? ZERO": "5", "CAL_DAYS? CAL": "10", "CAL_INTV?": "365",
        })
        args = self._args(monkeypatch, ["--status"])
        assert fc.run(args, dev=session.dev) == 0
        out = capsys.readouterr().out
        assert "5720A" in out and "DC22V" in out
        assert "STBY" in transport.log          # cleanup dropped the output

    def test_out_one_shot_sources_and_exits(self, monkeypatch):
        session, transport = make_session(**{"ONTIME?": "60"})
        args = self._args(monkeypatch, ["--out", "10V"])
        assert fc.run(args, dev=session.dev) == 0
        assert "OUT 1.0000000000E+01 V, 0.000000E+00 HZ" in transport.log

    def test_operate_without_a_value_is_rejected(self, monkeypatch, capsys):
        session, transport = make_session(**{"ONTIME?": "60"})
        args = self._args(monkeypatch, ["--operate"])
        assert fc.run(args, dev=session.dev) == 2
        assert "needs --out" in capsys.readouterr().out

    def test_no_action_flag_means_repl(self, monkeypatch):
        args = self._args(monkeypatch, [])
        assert fc._one_shot_requested(args) is False
        args = self._args(monkeypatch, ["--faults"])
        assert fc._one_shot_requested(args) is True

    def test_no_banner_suppresses_it(self, monkeypatch, capsys):
        session, transport = make_session(**{"FAULT?": "0"})
        args = self._args(monkeypatch, ["--no-banner", "--faults"])
        fc.run(args, dev=session.dev)
        assert "service console" not in capsys.readouterr().out

    def test_action_order_sources_before_it_energizes(self, monkeypatch):
        args = self._args(monkeypatch,
                          ["--status", "--operate", "--out", "10V"])
        assert fc.one_shot_actions(args) == ["out 10V", "oper", "status"]

    def test_freq_rides_along_with_out(self, monkeypatch):
        args = self._args(monkeypatch,
                          ["--out", "100V", "--freq", "1kHz"])
        assert fc.one_shot_actions(args) == ["out 100V 1kHz"]


class TestOneShotFailureContract:
    """A failed step must abandon the rest and exit non-zero.

    The reviewed defect: ``--out 2000V --operate`` printed a refusal, then
    energized whatever was staged *before* the run — a stale 10 V, or a stale
    1000 V — and exited 0. One-shot mode exists for scripting, so a refusal a
    script cannot detect is a refusal a script will drive straight through.
    """

    STALE_10V = {"OUT?": "1.000000E+01,V,0.000000E+00", "ONTIME?": "60"}

    def _args(self, monkeypatch, argv):
        return one_shot_args(monkeypatch, argv)

    def test_a_refused_source_never_energizes_the_stale_setpoint(
            self, monkeypatch, capsys):
        session, transport = make_session(**self.STALE_10V)
        args = self._args(monkeypatch,
                          ["--out", "2000V", "--operate", "--no-banner"])
        assert fc.run(args, dev=session.dev) == 1
        assert "OPER" not in transport.log
        out = capsys.readouterr().out
        assert "nothing was sent" in out
        assert "abandoning the remaining one-shot actions" in out

    def test_a_refused_source_abandons_the_later_read_actions(
            self, monkeypatch, capsys):
        session, transport = make_session(**self.STALE_10V)
        args = self._args(
            monkeypatch,
            ["--out", "2000V", "--status", "--faults", "--no-banner"])
        assert fc.run(args, dev=session.dev) == 1
        assert "RANGE?" not in transport.log        # status never ran

    def test_a_declined_confirmation_exits_non_zero(self, monkeypatch,
                                                    patch_input):
        session, transport = make_session(
            **{"OUT?": "1.000000E+02,V,0.000000E+00", "ONTIME?": "60"})
        patch_input("no")
        args = self._args(monkeypatch,
                          ["--out", "100V", "--operate", "--no-banner"])
        assert fc.run(args, dev=session.dev) == 1
        assert "OPER" not in transport.log

    def test_a_non_interactive_confirmation_exits_non_zero(self, monkeypatch,
                                                           patch_input):
        # Scripted, no tty: EOF at the typed-'yes' prompt. Nothing energizes,
        # and the caller can tell.
        session, transport = make_session(
            **{"OUT?": "1.000000E+02,V,0.000000E+00", "ONTIME?": "60"})
        patch_input(EOFError)
        args = self._args(monkeypatch,
                          ["--out", "100V", "--operate", "--no-banner"])
        assert fc.run(args, dev=session.dev) == 1
        assert "OPER" not in transport.log

    def test_a_refused_long_term_command_exits_non_zero(self, monkeypatch):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "1.000000E+02,V,0.000000E+00", "ONTIME?": "60"})
        args = self._args(monkeypatch, ["--diag", "D5700", "--no-banner"])
        assert fc.run(args, dev=session.dev) == 1
        assert not any("DIAG" in entry for entry in transport.log)

    def test_a_clean_run_still_exits_zero(self, monkeypatch):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_SETTLED | ISR_OPER,
            **{"OUT?": "1.000000E+01,V,0.000000E+00", "ONTIME?": "60"})
        args = self._args(monkeypatch,
                          ["--out", "10V", "--operate", "--no-banner"])
        assert fc.run(args, dev=session.dev) == 0
        assert "OPER" in transport.log


class TestLastError:
    def test_cleared_by_a_successful_command(self):
        session, transport = make_session()
        session.execute("frobnicate")
        assert session.last_error is not None
        session.execute("stby")
        assert session.last_error is None

    def test_set_by_an_unknown_command(self):
        session, transport = make_session()
        session.execute("frobnicate")
        assert isinstance(session.last_error, ConsoleError)

    def test_set_by_a_driver_failure(self):
        session, transport = make_session()
        session.execute("range")            # RANGE? unscripted -> timeout
        assert session.last_error is not None


def test_deferred_sigint_restores_the_previous_handler():
    import signal
    before = signal.getsignal(signal.SIGINT)
    with fc.deferred_sigint("CAL_ZERO") as state:
        assert state["hit"] is False
        assert signal.getsignal(signal.SIGINT) is not before
    assert signal.getsignal(signal.SIGINT) is before


class TestPromptSigint:
    """Ctrl-C at the prompt cancels the line; it must not end the session.

    The graceful-shutdown handler calls sys.exit, so without this the REPL's
    ``except KeyboardInterrupt`` branch was unreachable and a mistyped Ctrl-C
    mid-triage cost the whole session.
    """

    def test_installs_a_handler_that_raises_and_restores_it(self):
        import signal
        before = signal.getsignal(signal.SIGINT)
        with fc.prompt_sigint():
            installed = signal.getsignal(signal.SIGINT)
            assert installed is not before
            with pytest.raises(KeyboardInterrupt):
                installed(signal.SIGINT, None)
        assert signal.getsignal(signal.SIGINT) is before

    def test_the_repl_survives_a_cancelled_line(self, monkeypatch, capsys):
        session, transport = make_session()
        lines = iter([KeyboardInterrupt, "stby", "quit"])

        def fake_input(prompt=""):
            item = next(lines)
            if isinstance(item, type) and issubclass(item, BaseException):
                raise item()
            return item

        monkeypatch.setattr("builtins.input", fake_input)
        fc._run_repl(session)                 # must return, not raise
        out = capsys.readouterr().out
        assert "cancels the line" in out
        assert "STANDBY" in out               # the session carried on


class TestServiceIntervalsAgree:
    """One number from guide §11.6, spelled four ways, in four modules.

    ``ZERO_CAL_MAX_DAYS`` here, ``cal.procedure.DC_ZERO_INTERVAL_DAYS``,
    ``cal.engine.ZERO_INTERVAL_DAYS`` and
    ``cal.calreport.ZERO_CAL_INTERVAL_DAYS`` are all "the 5700A logs fault 1303
    at 30 days", and the warm-up figure is spelled three ways on top of that.

    They are **not** merged into one, deliberately: ``cal.engine`` and
    ``cal.calreport`` must import with no driver and no app present, and
    ``cal.procedure`` mirrors driver constants rather than importing them (see
    its "Import hygiene" note).  What that costs is exactly this -- nothing
    stops one of them being edited alone, and a run whose engine flags stale
    zeros at 30 days while its console reports them healthy at 45 is a
    disagreement no output makes visible.  Hence a guard rather than a merge.
    """

    def test_every_dc_zero_interval_is_the_same_number(self):
        from caldmm.cal.calreport import ZERO_CAL_INTERVAL_DAYS
        from caldmm.cal.engine import ZERO_INTERVAL_DAYS
        from caldmm.cal.procedure import DC_ZERO_INTERVAL_DAYS

        assert (fc.ZERO_CAL_MAX_DAYS
                == DC_ZERO_INTERVAL_DAYS
                == ZERO_INTERVAL_DAYS
                == ZERO_CAL_INTERVAL_DAYS
                == 30), "guide §11.6: dc zeros are specified every 30 days"

    def test_every_warm_up_figure_is_the_same_number(self):
        from caldmm.cal.engine import WARMUP_MINUTES as ENGINE_WARMUP
        from caldmm.cal.procedure import WARMUP_MINUTES as PROCEDURE_WARMUP
        from caldmm.drivers.fluke5700a import WARMUP_MAX_MINUTES

        assert (WARMUP_MAX_MINUTES
                == PROCEDURE_WARMUP
                == ENGINE_WARMUP
                == 30), ("guide §11.6: twice the off-time, to a maximum of "
                         "30 minutes")

    def test_the_22_v_line_is_the_same_number_everywhere(self):
        """The fourth copy of the other guide §19.2 constant.

        The driver's is the fourth: ``standby()`` sizes its de-energize
        deadline from it, and ``cal/`` cannot import a driver to share one.
        """
        from caldmm.apps.cal_verify import HIGH_VOLTAGE_V
        from caldmm.cal.procedure import MAX_LIVE_OUTPUT_V
        from caldmm.drivers.fluke5700a import (
            HIGH_VOLTAGE_V as DRIVER_HIGH_VOLTAGE_V)

        assert (fc.HIGH_VOLTAGE_V
                == MAX_LIVE_OUTPUT_V
                == HIGH_VOLTAGE_V
                == DRIVER_HIGH_VOLTAGE_V
                == 22.0)


# ── wideband, output editing, limits, hygiene (Phase 3) ───────────────

class TestWband:
    def test_bare_wband_queries_the_isr(self):
        from caldmm.drivers.fluke5700a import ISR_WBND
        session, transport = make_session(isr=ISR_REMOTE | ISR_WBND)
        session.execute("wband")
        assert transport.log[-1] == "ISR?"

    def test_wband_on_switches_the_out_envelope(self, capsys):
        session, transport = make_session()
        session.execute("wband on")
        assert "WBAND ON" in transport.log
        # 10 V dc is legal on the standard output and illegal on wideband;
        # the console must now refuse it offline.
        mark = len(transport.log)
        session.execute("out 10V")
        assert not any(entry.startswith("OUT ")
                       for entry in transport.log[mark:])
        assert session.last_error is not None

    def test_wband_off_warns_about_the_terminal_value(self, capsys):
        session, transport = make_session()
        session.execute("wband on")
        session.execute("wband off")
        out = capsys.readouterr().out
        assert "0 mV dc" in out


class TestIncrNewrefMult:
    def test_incr_magnitude_scales_the_prefix(self):
        session, transport = make_session(
            **{"ADJOUT?": "9.999990E+00,V,0.000000E+00"})
        session.execute("incr -10uV")
        assert "INCR -1.0000000000E-05" in transport.log

    def test_incr_frequency(self):
        session, transport = make_session(
            **{"ADJOUT?": "1.000000E+01,V,1.001000E+03"})
        session.execute("incr 1Hz")
        assert "INCR 1.0000000000E+00 HZ" in transport.log

    def test_incr_reports_the_adjusted_output(self, capsys):
        session, transport = make_session(
            **{"ADJOUT?": "9.999990E+00,V,0.000000E+00"})
        session.execute("incr -10uV")
        out = capsys.readouterr().out
        assert "ADJOUT?" in out and "newref" in out

    def test_newref(self):
        session, transport = make_session(
            **{"OUT?": "1.000000E+01,V,0.000000E+00"})
        session.execute("newref")
        assert "NEWREF" in transport.log

    def test_mult_warns_when_the_result_is_hazardous(self, capsys):
        session, transport = make_session(
            **{"OUT?": "1.900000E+02,V,0.000000E+00"})
        session.execute("mult 19")
        assert "MULT 1.9000000000E+01" in transport.log
        assert "22" in capsys.readouterr().out


class TestLiveEditsAreConfirmed:
    """M59: the one interlock the console advertises is not bypassed.

    ``Energizing >= 22 V requires a typed yes`` is what the how-to promises,
    and ``mult``/``incr`` edited a **live** output straight past it: an
    ``incr 100V`` typo at 20 V reached 120 V with nobody asked.
    """

    def test_mult_on_a_live_output_asks_before_it_sends(self, patch_input):
        prompts = patch_input("yes")
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "1.000000E+01,V,0.000000E+00"})
        session.execute("mult 4")
        assert "MULT 4.0000000000E+00" in transport.log
        assert any("40" in prompt for prompt in prompts), prompts

    def test_declining_leaves_the_output_alone(self, patch_input):
        patch_input("no")
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "1.000000E+01,V,0.000000E+00"})
        session.execute("mult 4")
        assert isinstance(session.last_error, ConsoleError)
        assert not any(line.startswith("MULT") for line in transport.log)

    def test_a_bare_y_is_not_enough(self, patch_input):
        patch_input("y")
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "1.000000E+01,V,0.000000E+00"})
        session.execute("mult 4")
        assert isinstance(session.last_error, ConsoleError)
        assert not any(line.startswith("MULT") for line in transport.log)

    def test_a_harmless_multiplication_is_not_gated(self):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "1.000000E+00,V,0.000000E+00"})
        session.execute("mult 2")
        assert "MULT 2.0000000000E+00" in transport.log

    def test_a_standby_output_is_not_gated(self):
        """In standby nothing reaches the terminals; ``oper`` still asks."""
        session, transport = make_session(
            **{"OUT?": "1.000000E+01,V,0.000000E+00"})
        session.execute("mult 4")
        assert "MULT 4.0000000000E+00" in transport.log

    def test_incr_past_the_threshold_asks(self, patch_input):
        prompts = patch_input("yes")
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"ADJOUT?": "2.000000E+01,V,0.000000E+00",
               "OUT?": "2.000000E+01,V,0.000000E+00"})
        session.execute("incr 100V")
        assert "INCR 1.0000000000E+02" in transport.log
        assert prompts

    def test_incr_declined_sends_nothing(self, patch_input):
        patch_input("no")
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"ADJOUT?": "2.000000E+01,V,0.000000E+00",
               "OUT?": "2.000000E+01,V,0.000000E+00"})
        session.execute("incr 100V")
        assert isinstance(session.last_error, ConsoleError)
        assert not any(line.startswith("INCR") for line in transport.log)

    def test_a_current_edit_past_one_amp_asks(self, patch_input):
        prompts = patch_input("yes")
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "5.000000E-01,A,0.000000E+00"})
        session.execute("mult 4")
        assert "MULT 4.0000000000E+00" in transport.log
        assert prompts, "no confirmation was asked for 2 A"

    def test_a_current_edit_declined_sends_nothing(self, patch_input):
        patch_input("no")
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "5.000000E-01,A,0.000000E+00"})
        session.execute("mult 4")
        assert isinstance(session.last_error, ConsoleError)
        assert not any(line.startswith("MULT") for line in transport.log)

    def test_a_frequency_step_is_not_an_amplitude_hazard(self):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"ADJOUT?": "1.000000E+02,V,1.001000E+03",
               "OUT?": "1.000000E+02,V,1.000000E+03"})
        session.execute("incr 1Hz")
        assert "INCR 1.0000000000E+00 HZ" in transport.log

    def test_newref_on_a_live_hazardous_output_asks(self, patch_input):
        prompts = patch_input("yes")
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "1.000000E+02,V,0.000000E+00"})
        session.execute("newref")
        assert "NEWREF" in transport.log
        assert prompts, "no confirmation was asked at 100 V"

    def test_newref_declined_sends_nothing(self, patch_input):
        patch_input("")
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "1.000000E+02,V,0.000000E+00"})
        session.execute("newref")
        assert isinstance(session.last_error, ConsoleError)
        assert "NEWREF" not in transport.log

    def test_an_unreadable_output_state_refuses_rather_than_guesses(self):
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER, **{"OUT?": "not a reading"})
        session.execute("mult 4")
        assert isinstance(session.last_error, ConsoleError)
        assert not any(line.startswith("MULT") for line in transport.log)


class TestLimitCommand:
    def test_query(self, capsys):
        session, transport = make_session(
            **{"LIMIT?": "2.20E+2,-1.0E+2,1.8E0,-1.2E0"})
        session.execute("limit")
        out = capsys.readouterr().out
        assert "voltage" in out and "current" in out
        assert "LIMIT?" in transport.log

    def test_set_voltage_limits(self):
        session, transport = make_session()
        session.execute("limit 25V -25V")
        assert ("LIMIT 2.5000000000E+01 V, -2.5000000000E+01 V"
                in transport.log)

    def test_mismatched_units_are_refused(self):
        session, transport = make_session()
        mark = len(transport.log)
        session.execute("limit 25V -1A")
        assert not any(entry.startswith("LIMIT")
                       for entry in transport.log[mark:])
        assert session.last_error is not None

    def test_ohms_are_refused(self):
        session, transport = make_session()
        session.execute("limit 10kohm -10kohm")
        assert session.last_error is not None


class TestIscrCommand:
    def test_reports_and_notes_the_clear(self, capsys):
        session, transport = make_session(**{"ISCR?": "8"})
        session.execute("iscr")
        out = capsys.readouterr().out
        assert "CHANGED" in out
        assert "CLEARED" in out


class TestWatchTransients:
    def test_a_bit_that_came_and_went_is_reported(self, capsys):
        # ISR reads the same twice; ISCR latched a BOOST change in between.
        session, transport = make_session(
            **{"ISCR?": ["0", "8"]})
        session.cmd_watch([], max_polls=2)
        out = capsys.readouterr().out
        assert "transient" in out


class TestCalTemp:
    def test_query_lists_the_activities(self, capsys):
        session, transport = make_session(
            **{"CAL_TEMP? CAL": "23.5", "CAL_TEMP? CHECK": "23.0",
               "CAL_TEMP? WBGAIN": "22.9", "CAL_TEMP? WBFLAT": "23.1"})
        session.execute("caltemp")
        out = capsys.readouterr().out
        assert "23.5" in out and "WBGAIN" in out

    def test_set(self, capsys):
        session, transport = make_session()
        session.execute("caltemp 23.4")
        assert "CAL_TEMP 23.4" in transport.log
        assert "23 °C" in capsys.readouterr().out


class TestCalIntervalConfidenceSetters:
    def test_calintv_set_is_flagged_nonvolatile(self, capsys):
        session, transport = make_session()
        session.execute("calintv 90")
        assert "CAL_INTV 90" in transport.log
        assert "NONVOLATILE" in capsys.readouterr().out

    def test_calconf_set(self):
        session, transport = make_session()
        session.execute("calconf 99")
        assert "CAL_CONF CONF99" in transport.log


class TestCalRng:
    def test_requires_service_mode(self):
        session, transport = make_session()
        mark = len(transport.log)
        session.execute("calrng DC2_2V 2V")
        assert not any(entry.startswith("CAL_RNG")
                       for entry in transport.log[mark:])
        assert session.last_error is not None

    def test_stages_a_range_point(self, capsys):
        session, transport = make_session(service=True)
        session.execute("calrng DC2_2V 2V")
        assert any(entry.startswith("CAL_RNG DC2_2V")
                   for entry in transport.log)
        assert "calrng null" in capsys.readouterr().out

    def test_store_needs_a_typed_confirmation(self, patch_input):
        session, transport = make_session(service=True)
        patch_input("no")
        session.execute("calrng store")
        assert "CAL_RNG STORE" not in transport.log


class TestWidebandCalCommands:
    def test_pgain_is_a_long_term_command_with_the_interlock(self, capsys):
        # Live 100 V output: the long-term interlock must refuse it.
        session, transport = make_session(
            isr=ISR_REMOTE | ISR_OPER,
            **{"OUT?": "1.000000E+02,V,0.000000E+00"})
        session.execute("calwbgain pgain")
        assert not any("CAL_WBGAIN" in entry for entry in transport.log)
        assert "refusing" in capsys.readouterr().out

    def test_store_declined_sends_nothing(self, patch_input):
        session, transport = make_session()
        patch_input("no")
        session.execute("calwbgain store")
        assert not any("CAL_WBGAIN" in entry for entry in transport.log)

    def test_wbflat_start_reports_the_sourced_point(self, capsys):
        session, transport = make_session(
            **{"OUT?": "1.000000E+00,V,1.000000E+06"})
        session.execute("calwbflat start")
        assert "CAL_WBFLAT START" in transport.log
        out = capsys.readouterr().out
        assert "incr" in out and "calwbflat null" in out

    def test_wbflat_termination_condition_is_announced(self, capsys):
        # OUT? returning 0,V,0 means every point is calibrated (guide §9).
        session, transport = make_session(
            **{"OUT?": "0.000000E+00,V,0.000000E+00"})
        session.execute("calwbflat null")
        out = capsys.readouterr().out
        assert "every" in out and "store" in out


class TestLocalCommand:
    def test_sends_gtl(self, capsys):
        session, transport = make_session()
        session.execute("local")
        assert ("local", None) in transport.events
        assert "front panel" in capsys.readouterr().out


# ── interface selection (GPIB or RS-232C) ─────────────────────────────

class TestInterfaceSelection:
    """``--port`` opens the rear-panel serial port instead of GPIB."""

    def _run(self, monkeypatch, argv):
        """Parse *argv* and run far enough to see how the session is opened."""
        monkeypatch.setattr(fc, "setup_graceful_shutdown",
                            lambda cleanup: None)
        args = fc.build_parser().parse_args(argv)
        opened = MagicMock()
        opened.startup_faults = []
        with patch.object(fc, "Fluke5700A", return_value=opened) as driver:
            fc.run(args, dev=None)
        return driver.call_args

    def test_the_default_is_still_gpib(self, monkeypatch):
        call = self._run(monkeypatch, ["--no-banner", "--faults"])
        assert call.args == (f57.DEFAULT_GPIB_ADDRESS, f57.DEFAULT_GPIB_BUS)
        assert "serial_port" not in call.kwargs

    def test_port_opens_a_serial_session(self, monkeypatch):
        call = self._run(monkeypatch,
                         ["--port", "/dev/ttyUSB0", "--no-banner", "--faults"])
        assert call.kwargs["serial_port"] == "/dev/ttyUSB0"
        assert call.kwargs["serial_baud"] == f57.DEFAULT_SERIAL_BAUD
        assert call.args == ()

    def test_baud_rides_along_with_port(self, monkeypatch):
        call = self._run(monkeypatch,
                         ["--port", "/dev/ttyS0", "--baud", "19200",
                          "--no-banner", "--faults"])
        assert call.kwargs["serial_baud"] == 19200

    def test_an_address_and_a_port_are_mutually_exclusive(self):
        with pytest.raises(SystemExit) as excinfo:
            fc.build_parser().parse_args(
                ["--address", "4", "--port", "/dev/ttyUSB0"])
        assert excinfo.value.code == 2

    def test_a_baud_the_calibrator_lacks_is_rejected(self):
        with pytest.raises(SystemExit):
            fc.build_parser().parse_args(["--baud", "57600"])

    def test_a_failed_open_names_the_serial_port(self, monkeypatch, capsys):
        monkeypatch.setattr(fc, "setup_graceful_shutdown",
                            lambda cleanup: None)
        args = fc.build_parser().parse_args(["--port", "/dev/ttyUSB0"])
        with patch.object(fc, "Fluke5700A",
                          side_effect=OSError("no such device")):
            assert fc.run(args, dev=None) == 1
        assert "serial /dev/ttyUSB0 at 9600 baud" in capsys.readouterr().err

"""Tests for the adjustment-procedure framework and the 5700A artifact cal.

Offline only.  The instrument is a **real** :class:`Fluke5700A` driving
:class:`tests.drivers.fake_gpib.FakeTransport`, so every assertion about "what
went on the wire" is about what the driver really emits -- not about what a
mock was told to record.  The operator is a fake console with scripted answers.

Fixtures are built through the real constructors on purpose.  An
:class:`ExternalStandard` validates in ``__post_init__``, so a test cannot
fabricate a standard the operator interface would have refused, and a
:class:`ProcedureResult` is only ever produced by :func:`run_procedure`.
"""

import subprocess
import sys
import textwrap

import pytest

from caldmm.cal import procedure as proc
from caldmm.cal.procedure import (
    ARTIFACT_STANDARDS,
    CAL_REF_ORDER,
    AdjustStep,
    CalibrationSwitchLockout,
    ExternalStandard,
    Fluke5700AArtifactCal,
    Fluke5700ACalCheck,
    Fluke5700ADcZeros,
    PreflightError,
    Procedure,
    ProcedureContext,
    ProcedureResult,
    SafetyError,
    STANDARDS_BY_KEY,
    StandardValueError,
    StepOrderError,
    StepOutcome,
    available_procedures,
    get_procedure,
    parse_certified_value,
    parse_temperature,
    parse_uncertainty,
    procedures_for,
    prompt_standard,
    run_procedure,
)
from caldmm.drivers import fluke5700a as f57
from caldmm.drivers.fluke5700a import Fluke5700A, ISR_REMOTE
from caldmm.lib.console import OperatorConsole

from tests.drivers.fake_gpib import FakeTransport


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


# ── the operator ──────────────────────────────────────────────────────

class FakeConsole:
    """An operator with scripted answers, and a transcript.

    Signature-compatible with :class:`caldmm.lib.console.OperatorConsole` --
    ``show``, ``prompt``, ``confirm_ok``, ``confirm_explicit`` and ``ask``.
    ``TestConsoleSeam`` pins that compatibility against the real class.
    """

    def __init__(self, answers=None, *, ok=True, explicit=True):
        self.answers = list(answers or [])
        self.shown: "list[tuple[str, ...]]" = []
        self.prompted: "list[str]" = []
        self.asked: "list[str]" = []
        self.confirm_ok_calls: "list[str]" = []
        self.confirm_explicit_calls: "list[str]" = []
        self.hints: "list[str | None]" = []
        self._ok = ok
        self._explicit = explicit

    # -- OperatorConsole surface --

    def show(self, banner, *body, panel_msg=None):
        self.shown.append((banner, *body))

    def prompt(self, banner, *body, panel_msg=None, action=""):
        self.prompted.append(banner)
        self.shown.append((banner, *body))

    def confirm_ok(self, what="this measurement", *, hint=None):
        self.confirm_ok_calls.append(what)
        return self._ok(what) if callable(self._ok) else bool(self._ok)

    def confirm_explicit(self, what, *, hint=None):
        self.confirm_explicit_calls.append(what)
        self.hints.append(hint)
        if isinstance(self._explicit, BaseException):
            raise self._explicit
        return (self._explicit(what) if callable(self._explicit)
                else bool(self._explicit))

    # -- free-text entry --

    def ask(self, question):
        self.asked.append(question)
        if not self.answers:
            raise AssertionError(f"console asked an unscripted question: "
                                 f"{question!r}")
        return self.answers.pop(0)

    # -- helpers for assertions --

    @property
    def text(self):
        return "\n".join(" | ".join(str(p) for p in entry)
                         for entry in self.shown)


# ── the instrument ────────────────────────────────────────────────────

GOOD_IDN = "FLUKE,5720A,5248000,1.0+B+*"

#: Two output-shift rows that :mod:`caldmm.cal.calreport` really parses --
#: verified against the parser, not invented.
def _report_lines(zero_pct, fs_pct):
    return [
        "FLUKE CORPORATION   S/N 5248000   5700A OUTPUT CALIBRATION",
        "PRINTED ON  1/02/26 AT 03:04:05 5700A  S/N 5248000",
        "DC VOLTAGE OUTPUT SHIFTS",
        "RANGE Point     Zero Shift       Full Scale Shift          Spec   Shift",
        "(+/-) (% spec)",
        f"220 mV +FS     0.00003 mV    -0.00041 mV  -1.87   ppm   9.91 ppm  {zero_pct}",
        f"2.2 V +FS      0.00002 mV     0.00021 mV   0.95   ppm   9.10 ppm  {fs_pct}",
    ]


def _block(lines):
    return ['"', *lines, '"']


SLST_BLOCK = _block([
    "1", "DC2_2V,1",
    "2.00E+00,0.00E+00,1.10E-06,2.30E-06,1.15E+00,9.10E+00,1.26E+01",
])


def default_responses(**overrides):
    """Everything a full artifact-cal run queries, with plausible answers.

    ``*ESR?`` answers ``1`` (the OPC bit) so every long-term command completes
    on its first poll -- the driver's own ``_run_long_term`` loop is exercised,
    just quickly.
    """
    responses = {
        "FAULT?": "0",
        "*IDN?": GOOD_IDN,
        "*OPT?": '"5725A Attached"',
        "ISR?": str(ISR_REMOTE),
        "OUT?": "0.0E+00,V,0",
        "*ESR?": "1",
        "STATE?": "0,Idle",
        "ONTIME?": "60",
        "CAL_DAYS? ZERO": "5",
        "CAL_RPT? CAL": [_block(_report_lines("-18.88", "10.44")),
                         _block(_report_lines("-2.10", "1.02"))],
        "CAL_SLST? CAL": [SLST_BLOCK, SLST_BLOCK],
        "CAL_RPT? CHECK": [_block(_report_lines("-18.88", "10.44")),
                           _block(_report_lines("-2.10", "1.02"))],
        "CAL_SLST? CHECK": [SLST_BLOCK, SLST_BLOCK],
    }
    responses.update(overrides)
    return responses


def opened(**overrides):
    """``(driver, transport)`` for a live, opened calibrator session."""
    transport = FakeTransport(default_responses(**overrides))
    return Fluke5700A(transport=transport), transport


# ── standards, built the way the operator would ───────────────────────

def make_standard(key, certified, uncertainty_text="1.5 ppm"):
    spec = STANDARDS_BY_KEY[key]
    return ExternalStandard(
        spec=spec, certified=certified,
        uncertainty=parse_uncertainty(uncertainty_text, spec, certified),
        serial="test")


def artifact_standards():
    return {
        "10V": make_standard("10V", 10.00013, "1.5 ppm"),
        "10KOHM": make_standard("10KOHM", 9.99987, "4 ppm"),
        "1OHM": make_standard("1OHM", 1.00026, "10 ppm"),
    }


def make_context(device, console=None, **kwargs):
    kwargs.setdefault("ambient_c", 23.5)
    kwargs.setdefault("standards", artifact_standards())
    return ProcedureContext(device=device, console=console or FakeConsole(),
                            **kwargs)


def mutating(transport):
    """Only the commands that change the instrument, in order.

    Queries and the session boilerplate are filtered out so the sequence can be
    compared against the guide §14.6 recipe directly.
    """
    return [cmd for cmd in transport.log
            if cmd.startswith("CAL_") and "?" not in cmd]


# ══════════════════════════════════════════════════════════════════════
# Operator entry: parsing and sanity
# ══════════════════════════════════════════════════════════════════════

class TestValueEntry:
    def test_bare_number_is_the_standards_own_unit(self):
        spec = STANDARDS_BY_KEY["10KOHM"]
        assert parse_certified_value("9.99987", spec) == pytest.approx(9.99987)

    def test_a_unit_may_override(self):
        # A 10 kOhm certificate written in ohms.
        spec = STANDARDS_BY_KEY["10KOHM"]
        assert parse_certified_value("9999.87 ohm", spec) == pytest.approx(
            9.99987)

    def test_millivolts_scale(self):
        spec = STANDARDS_BY_KEY["10V"]
        assert parse_certified_value("10000.13 mV", spec) == pytest.approx(
            10.00013)

    def test_a_foreign_unit_is_refused(self):
        with pytest.raises(StandardValueError):
            parse_certified_value("10 A", STANDARDS_BY_KEY["10V"])

    def test_uncertainty_needs_a_unit(self):
        # 1.5 ppm and 1.5 V differ by six orders of magnitude and this number
        # is written into the calibration, so there is no safe default.
        with pytest.raises(StandardValueError) as exc:
            parse_uncertainty("1.5", STANDARDS_BY_KEY["10V"], 10.0)
        assert "no unit" in str(exc.value)

    @pytest.mark.parametrize("text,expected", [
        ("1.5 ppm", 1.5e-5),
        ("15 uV", 1.5e-5),
        ("0.000015 V", 1.5e-5),
        ("15000 ppb", 1.5e-4),
    ])
    def test_uncertainty_units(self, text, expected):
        assert parse_uncertainty(text, STANDARDS_BY_KEY["10V"],
                                 10.0) == pytest.approx(expected)

    def test_temperature_band_is_enforced(self):
        assert parse_temperature("23.5 C") == 23.5
        with pytest.raises(StandardValueError):
            parse_temperature("40")


class TestStandardSanity:
    def test_ten_volts_typed_as_ten_thousand_is_rejected(self):
        with pytest.raises(StandardValueError) as exc:
            ExternalStandard(STANDARDS_BY_KEY["10V"], 10000.0, 1e-5)
        assert "decade" in str(exc.value)

    def test_one_ohm_typed_as_a_kilohm_is_rejected(self):
        with pytest.raises(StandardValueError):
            ExternalStandard(STANDARDS_BY_KEY["1OHM"], 1000.0, 1e-5)

    def test_the_uncertainty_is_required(self):
        with pytest.raises(StandardValueError) as exc:
            ExternalStandard(STANDARDS_BY_KEY["10V"], 10.00013, 0.0)
        assert "required" in str(exc.value)

    def test_an_absurd_uncertainty_is_rejected(self):
        # "1.5" typed where "1.5 ppm" was meant, entered as volts.
        with pytest.raises(StandardValueError) as exc:
            ExternalStandard(STANDARDS_BY_KEY["10V"], 10.00013, 1.5)
        assert "ppm" in str(exc.value)

    def test_the_wrong_sign_is_rejected(self):
        with pytest.raises(StandardValueError) as exc:
            ExternalStandard(STANDARDS_BY_KEY["10V"], -10.00013, 1e-5)
        assert "sign" in str(exc.value)

    def test_derived_quantities(self):
        std = make_standard("10V", 10.00013, "1.5 ppm")
        assert std.deviation_ppm == pytest.approx(13.0, abs=0.1)
        assert std.uncertainty_ppm == pytest.approx(1.5)
        assert std.standard_uncertainty == pytest.approx(std.uncertainty / 2)


class TestPromptStandard:
    def test_it_loops_until_the_entry_is_sane(self):
        console = FakeConsole(answers=[
            "10000", "1.5 ppm", "",            # decade error -> rejected
            "10.00013", "1.5 ppm", "SN 732A-1",
        ])
        standard = prompt_standard(console, STANDARDS_BY_KEY["10V"])
        assert standard.certified == pytest.approx(10.00013)
        assert standard.uncertainty_ppm == pytest.approx(1.5)
        assert standard.serial == "SN 732A-1"
        assert any("rejected" in entry[0] for entry in console.shown)

    def test_it_asks_for_the_uncertainty_every_time(self):
        console = FakeConsole(answers=["10.00013", "1.5 ppm", ""])
        prompt_standard(console, STANDARDS_BY_KEY["10V"])
        assert any("uncertainty" in q for q in console.asked)

    def test_it_gives_up_rather_than_looping_forever(self):
        console = FakeConsole(answers=["10000", "1.5 ppm", ""] * 2)
        with pytest.raises(StandardValueError):
            prompt_standard(console, STANDARDS_BY_KEY["10V"], attempts=2)


# ══════════════════════════════════════════════════════════════════════
# The mandatory CAL_REF order
# ══════════════════════════════════════════════════════════════════════

class TestOrderEnforcement:
    def test_the_order_is_the_guides(self):
        assert [key for _, key, _ in CAL_REF_ORDER] == [
            "10V", "10V", "10KOHM", "1OHM"]
        assert [sign for _, _, sign in CAL_REF_ORDER] == [1, -1, 1, 1]

    def test_a_step_out_of_order_never_reaches_the_wire(self):
        cal, transport = opened()
        ctx = make_context(cal)
        procedure = Fluke5700AArtifactCal()
        # CAL_TEMP done, none of the references: jumping straight to the 1 Ohm
        # step is the mistake the instrument answers with fault 852.
        ctx.record(proc.StepResult(
            step_id="cal-temp", outcome=StepOutcome.EXECUTED,
            started_at=proc._now(), finished_at=proc._now()))
        transport.log.clear()

        with pytest.raises(StepOrderError) as exc:
            procedure.cal_ref_step(ctx, "ref-1ohm")

        # The whole point: refused *here*, not by fault 852 after the
        # calibrator has spent a long-term command on it.
        assert not any("CAL_REF" in cmd for cmd in transport.log)
        assert "852" in str(exc.value)
        assert "Nothing was sent" in str(exc.value)

    def test_cal_temp_must_come_first(self):
        cal, transport = opened()
        ctx = make_context(cal)
        procedure = Fluke5700AArtifactCal()
        transport.log.clear()
        with pytest.raises(StepOrderError) as exc:
            procedure.cal_ref_step(ctx, "ref-10v-pos")
        assert "CAL_TEMP" in str(exc.value)
        assert transport.log == []

    def test_a_declined_step_blocks_everything_downstream(self):
        # The realistic failure: the operator skips the lead-reversal step.
        def decline_the_negative(what):
            return "ref-10v-neg" not in what

        console = FakeConsole(ok=decline_the_negative)
        cal, transport = opened()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, console))

        assert result.failed
        sent = mutating(transport)
        assert "CAL_REF -1.0000130000E+01 V;*OPC" not in sent
        # ... and the 10 kOhm step that follows it was refused before the wire.
        assert not any("KOHM" in cmd for cmd in sent)
        assert "CAL_ADJ;*OPC" not in sent
        failed = [s for s in result.steps if s.outcome is StepOutcome.FAILED]
        assert [s.step_id for s in failed] == ["ref-10kohm"]
        assert "StepOrderError" in failed[0].error

    def test_the_same_step_cannot_be_taken_twice(self):
        cal, _ = opened()
        ctx = make_context(cal)
        procedure = Fluke5700AArtifactCal()
        ctx.record(proc.StepResult(
            step_id="cal-temp", outcome=StepOutcome.EXECUTED,
            started_at=proc._now(), finished_at=proc._now()))
        ctx.record(proc.StepResult(
            step_id="ref-10v-pos", outcome=StepOutcome.EXECUTED,
            started_at=proc._now(), finished_at=proc._now()))
        with pytest.raises(StepOrderError):
            procedure.require_order(ctx, "ref-10v-pos")

    def test_cal_adj_refuses_a_partial_reference_set(self):
        cal, transport = opened()
        ctx = make_context(cal)
        procedure = Fluke5700AArtifactCal()
        step = procedure._adjust_step()
        transport.log.clear()
        with pytest.raises(StepOrderError):
            step.action(ctx, step)
        assert transport.log == []


# ══════════════════════════════════════════════════════════════════════
# The full sequence
# ══════════════════════════════════════════════════════════════════════

#: Guide §14.6, as this driver emits it.  ``CAL_STORE`` carries the manual's
#: ``*WAI``; the long-term commands carry ``;*OPC`` instead.
#:
#: ``*WAI`` is an *instrument-side* barrier — it returns to the controller
#: immediately and only stops the 5700A from acting on later commands — so it
#: is not itself what would tie up the bus.  What ties up the bus is what has
#: to follow it: ``cmd;*WAI`` is only useful with a *read* behind it (the
#: driver's ``CAL_STORE;*WAI`` is followed by a ``FAULT?`` drain), and that
#: read blocks for the whole operation and reports no progress.  Guide §7.5
#: says outright to "prefer the ``*OPC`` + ``*ESR?``-polling form for long-term
#: commands", §14.6 does not put a ``*WAI`` on ``CAL_ADJ`` either, and
#: ``_run_long_term`` accumulates the ESR across polls so an error raised
#: *during* the operation is not discarded.  ``CAL_STORE`` is not a long-term
#: command, so it keeps its ``*WAI``.
EXPECTED_SEQUENCE = [
    "CAL_TEMP 23.5",
    "CAL_REF 1.0000130000E+01 V;*OPC",
    "CAL_REF -1.0000130000E+01 V;*OPC",
    "CAL_REF 9.9998700000E+00 KOHM;*OPC",
    "CAL_REF 1.0002600000E+00 OHM;*OPC",
    "CAL_ADJ;*OPC",
    "CAL_STORE;*WAI",
]


class TestFullArtifactCal:
    def test_the_committing_sequence_matches_the_guide(self):
        cal, transport = opened()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, commit=True))
        assert not result.failed, result.error
        assert mutating(transport) == EXPECTED_SEQUENCE
        assert result.committed
        assert result.complete

    def test_the_declared_commands_are_what_actually_goes_on_the_wire(self):
        # AdjustStep.commands drives the dry-run preview.  If it drifted from
        # what the driver really emits, the preview would be a lie.
        cal, transport = opened()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, commit=True))
        declared = [cmd for step in result.steps for cmd in step.commands
                    if "?" not in cmd]
        assert declared == mutating(transport)

    def test_the_reports_are_captured_before_and_after_and_diffed(self):
        cal, transport = opened()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, commit=True))
        assert transport.log.count("CAL_RPT? CAL") == 2
        assert transport.log.count("CAL_SLST? CAL") == 2
        assert result.before_report is not None
        assert result.after_report is not None
        assert result.report_diff is not None
        moved = {(d.range_label, d.point): (d.before_percent, d.after_percent)
                 for d in result.report_diff.deltas}
        assert moved[("220 mV", "+FS")] == (-18.88, -2.10)
        assert result.report_diff.worst()[0].improved

    def test_per_range_cal_shift_is_captured_when_asked_for(self):
        # CAL_SLST? already carries every range, so shift_ranges is empty by
        # default; naming one adds the per-range CAL_SHIFT? on top.
        shift_block = _block([
            "DC2_2V,1",
            "2.00E+00,0.00E+00,1.10E-06,2.30E-06,1.15E+00,9.10E+00,1.26E+01",
        ])
        cal, transport = opened(
            **{"CAL_SHIFT? CAL, DC2_2V": [shift_block, shift_block]})
        result = run_procedure(
            Fluke5700AArtifactCal(),
            make_context(cal, commit=True, shift_ranges=("DC2_2V",)))
        assert not result.failed, result.error
        assert transport.log.count("CAL_SHIFT? CAL, DC2_2V") == 2
        captures = result.detail["captures"]
        assert captures["before"]["range_shifts"]["DC2_2V"].range_id == "DC2_2V"
        assert captures["after"]["range_shifts"]["DC2_2V"].range_id == "DC2_2V"

    def test_a_report_that_will_not_print_warns_rather_than_aborting(self):
        # Losing the report is bad; abandoning an artifact calibration with
        # computed-but-uncommitted constants is worse.
        cal, transport = opened(**{"CAL_RPT? CAL": [], "CAL_SLST? CAL": []})
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, commit=True))
        assert not result.failed, result.error
        assert mutating(transport) == EXPECTED_SEQUENCE
        assert any("CAL_RPT? CAL failed" in w for w in result.warnings)
        assert result.before_report is None
        assert result.report_diff is None

    def test_the_after_report_is_captured_before_the_commit(self):
        # That ordering is what makes a dry run informative: the numbers a
        # CAL_STORE would freeze are already on the record.
        cal, transport = opened()
        run_procedure(Fluke5700AArtifactCal(), make_context(cal, commit=True))
        assert transport.log.index("CAL_RPT? CAL", 1) < transport.log.index(
            "CAL_STORE;*WAI")

    def test_the_standards_and_their_uncertainties_land_in_the_result(self):
        cal, _ = opened()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, commit=True))
        by_key = {s.key: s for s in result.standards}
        assert set(by_key) == {"10V", "10KOHM", "1OHM"}
        assert by_key["10V"].uncertainty_ppm == pytest.approx(1.5)
        assert by_key["1OHM"].uncertainty_ppm == pytest.approx(10.0)
        assert result.ambient_c == 23.5
        assert "1.50 ppm" in result.summary()

    def test_the_negative_step_uses_the_same_standard_negated(self):
        cal, _ = opened()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, commit=True))
        pos = next(s for s in result.steps if s.step_id == "ref-10v-pos")
        neg = next(s for s in result.steps if s.step_id == "ref-10v-neg")
        assert pos.detail["value"] == -neg.detail["value"]
        assert pos.detail["standard"] is neg.detail["standard"]

    def test_the_operator_is_told_to_reverse_the_leads(self):
        console = FakeConsole()
        cal, _ = opened()
        run_procedure(Fluke5700AArtifactCal(),
                      make_context(cal, console, commit=True))
        assert "REVERSE THE LEADS" in console.text

    def test_the_switch_prompt_names_the_physical_switch(self):
        console = FakeConsole()
        cal, _ = opened()
        run_procedure(Fluke5700AArtifactCal(),
                      make_context(cal, console, commit=True))
        assert "CALIBRATION switch to ENABLE" in console.text
        assert "back to NORMAL" in console.text


# ══════════════════════════════════════════════════════════════════════
# Dry run
# ══════════════════════════════════════════════════════════════════════

class TestDryRun:
    def test_it_is_the_default(self):
        cal, _ = opened()
        assert make_context(cal).dry_run is True

    def test_it_commits_nothing_and_does_everything_else(self):
        cal, transport = opened()
        result = run_procedure(Fluke5700AArtifactCal(), make_context(cal))

        assert not result.failed, result.error
        # The whole computation ran ...
        assert mutating(transport) == EXPECTED_SEQUENCE[:-1]
        # ... and nothing was committed.
        assert "CAL_STORE;*WAI" not in transport.log
        assert not any("CAL_STORE" in cmd for cmd in transport.log)
        assert result.committed is False

    def test_it_records_what_would_have_been_committed(self):
        cal, _ = opened()
        result = run_procedure(Fluke5700AArtifactCal(), make_context(cal))
        assert result.would_commit == ("CAL_STORE;*WAI",)
        store = next(s for s in result.steps if s.step_id == "store")
        assert store.outcome is StepOutcome.SKIPPED_DRY_RUN
        assert store.committed is False
        assert "would have sent" in result.summary()

    def test_the_post_adjust_report_is_still_captured(self):
        # Guide §14.6: omitting CAL_STORE "collects performance data without
        # committing constants".  That data is the point of the dry run.
        cal, _ = opened()
        result = run_procedure(Fluke5700AArtifactCal(), make_context(cal))
        assert result.after_report is not None
        assert result.report_diff is not None

    def test_the_operator_is_never_sent_to_the_switch(self):
        console = FakeConsole()
        cal, _ = opened()
        run_procedure(Fluke5700AArtifactCal(), make_context(cal, console))
        assert "SKIPPED" in console.text or "dry run" in console.text
        assert console.confirm_explicit_calls == []

    def test_a_dry_run_of_the_zeros_holds_cal_zero_back(self):
        cal, transport = opened()
        result = run_procedure(Fluke5700ADcZeros(),
                               ProcedureContext(device=cal,
                                                console=FakeConsole()))
        assert "CAL_ZERO;*OPC" not in transport.log
        assert result.would_commit == ("CAL_ZERO;*OPC",)


class TestStoreGating:
    def test_store_requires_the_typed_confirmation(self):
        console = FakeConsole(explicit=False)         # types anything but yes
        cal, transport = opened()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, console, commit=True))

        assert console.confirm_explicit_calls          # it *was* asked
        assert "NON-VOLATILE" in console.confirm_explicit_calls[0]
        assert "CAL_STORE;*WAI" not in transport.log
        store = next(s for s in result.steps if s.step_id == "store")
        assert store.outcome is StepOutcome.SKIPPED_DECLINED
        assert result.committed is False

    def test_the_decline_hint_says_the_skip_is_permanent(self):
        # confirm_explicit's default hint promises "Enter to repeat", but the
        # runner treats a False as a permanent skip of an irreversible step.
        console = FakeConsole(explicit=False)
        cal, _ = opened()
        run_procedure(Fluke5700AArtifactCal(),
                      make_context(cal, console, commit=True))
        hint = console.hints[0]
        assert hint is not None and "permanently" in hint
        assert "repeat" not in hint

    def test_a_declined_commit_is_reported_as_held_back(self):
        # would_commit answers "what did this run not do?" -- and a command
        # that did not go out did not go out, whatever the reason.
        console = FakeConsole(explicit=False)
        cal, _ = opened()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, console, commit=True))
        assert result.would_commit == ("CAL_STORE;*WAI",)
        assert "would have sent" in result.summary()
        # ... and the run must not look like a clean success.
        assert result.failed is False          # nothing raised
        assert result.complete is False        # but it did not all run
        assert result.committed is False

    def test_a_declined_reference_step_is_not_called_a_commit(self):
        # It was held back too, but printing a CAL_REF under "would have sent"
        # would read as though sourcing a reference were a commit.
        console = FakeConsole(ok=lambda what: "ref-10v-neg" not in what)
        cal, _ = opened()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, console, commit=True))
        assert result.would_commit == ()
        assert "would have sent" not in result.summary()
        # ... and it is still on the record, in the step that held it back.
        declined = next(s for s in result.steps
                        if s.step_id == "ref-10v-neg")
        assert declined.skipped_commands == (
            "CAL_REF -1.0000130000E+01 V;*OPC",)

    def test_a_declined_commit_tells_the_operator_to_restore_the_switch(self):
        console = FakeConsole(explicit=False)
        cal, _ = opened()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, console, commit=True))
        assert "SET IT BACK TO NORMAL" in console.text
        assert any("NORMAL" in w for w in result.warnings)

    def test_ctrl_c_at_the_typed_confirmation_keeps_the_audit_trail(self):
        # CAL_TEMP, four CAL_REFs and CAL_ADJ have already run by this point.
        # Losing the whole record to a KeyboardInterrupt -- which is what a
        # closed stdin becomes, so a piped session reaches it -- would destroy
        # the audit trail for an operation that already happened.
        console = FakeConsole(explicit=KeyboardInterrupt())
        cal, transport = opened()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, console, commit=True))

        assert result.interrupted
        assert [s.step_id for s in result.steps][:3] == [
            "brief", "capture-before", "cal-temp"]
        assert "CAL_ADJ;*OPC" in transport.log
        assert "CAL_STORE;*WAI" not in transport.log
        assert result.committed is False
        assert "store" in result.unreached
        # ... and they are still told about the switch.
        assert "SET IT BACK TO NORMAL" in console.text

    def test_store_refuses_when_cal_adj_did_not_run(self):
        cal, transport = opened()
        ctx = make_context(cal, commit=True)
        procedure = Fluke5700AArtifactCal()
        step = procedure._store_step()
        transport.log.clear()
        with pytest.raises(StepOrderError):
            step.action(ctx, step)
        assert transport.log == []


# ══════════════════════════════════════════════════════════════════════
# The CALIBRATION switch
# ══════════════════════════════════════════════════════════════════════

def faults_once(transport, code, after):
    """A ``FAULT?`` responder that reports *code* once, after *after* is sent.

    Models the real thing: the queue is empty until the refused command lands,
    then holds one fault, then is empty again.
    """
    state = {"given": False}

    def responder(_cmd):
        if state["given"]:
            return "0"
        if any(cmd.startswith(after) for cmd in transport.log):
            state["given"] = True
            return str(code)
        return "0"

    return responder


class TestCalibrationSwitch:
    def test_fault_845_names_the_physical_switch(self):
        transport = FakeTransport(default_responses(
            **{"EXPLAIN? 845": '"Cal Switch Must Be Enabled"'}))
        transport.responses["FAULT?"] = faults_once(transport, 845,
                                                    "CAL_STORE")
        cal = Fluke5700A(transport=transport)
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, commit=True))

        assert result.failed
        store = next(s for s in result.steps if s.step_id == "store")
        message = store.error
        # This message is the entire user experience of the failure.
        assert "CALIBRATION switch" in message
        assert "NORMAL" in message
        assert "NOTHING WAS STORED" in message
        assert "back of the instrument" in message
        assert "845" in message
        assert result.committed is False

    def test_a_refusal_leaves_nvram_provably_untouched(self):
        # A drained 845 is the instrument saying it refused the command, so
        # "not committed" is a fact here, not a guess.
        transport = FakeTransport(default_responses())
        transport.responses["FAULT?"] = faults_once(transport, 845,
                                                    "CAL_STORE")
        cal = Fluke5700A(transport=transport)
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, commit=True))
        assert result.nvram_uncertain is False
        assert result.nvram_state is proc.NvramState.UNTOUCHED
        assert "committed to NVRAM: no" in result.summary()

    def test_a_refused_command_is_recorded_as_sent(self):
        # CAL_STORE;*WAI really was on the wire — the calibrator answered it
        # with fault 845. Filing it under "commands that were NOT sent" would
        # be the same class of mislabelling as calling an unread outcome "not
        # committed". The definite "no" comes from nvram, not from this field.
        transport = FakeTransport(default_responses())
        transport.responses["FAULT?"] = faults_once(transport, 845,
                                                    "CAL_STORE")
        cal = Fluke5700A(transport=transport)
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, commit=True))

        assert "CAL_STORE;*WAI" in transport.log
        store = next(s for s in result.steps if s.step_id == "store")
        assert store.commands == ("CAL_STORE;*WAI",)
        assert store.skipped_commands == ()
        assert store.nvram is proc.NvramState.UNTOUCHED
        assert result.would_commit == ()

    def test_a_guard_refusal_is_recorded_as_unsent(self):
        # The other half: SafetyError is raised before any I/O, so the command
        # genuinely never left.
        transport = FakeTransport(default_responses())

        def out(_cmd):
            if any(c.startswith("CAL_TEMP") for c in transport.log):
                return "5.0E+02,V,0"
            return "0.0E+00,V,0"

        transport.responses["OUT?"] = out
        cal = Fluke5700A(transport=transport)
        result = run_procedure(Fluke5700AArtifactCal(), make_context(cal))

        failed = next(s for s in result.steps
                      if s.outcome is StepOutcome.FAILED)
        assert failed.commands == ()
        assert failed.skipped_commands == (
            "CAL_REF 1.0000130000E+01 V;*OPC",)
        assert not any("CAL_REF" in cmd for cmd in transport.log)

    def test_a_refusal_still_tells_the_operator_about_the_switch(self):
        console = FakeConsole()
        transport = FakeTransport(default_responses())
        transport.responses["FAULT?"] = faults_once(transport, 845,
                                                    "CAL_STORE")
        cal = Fluke5700A(transport=transport)
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, console, commit=True))
        assert "SET IT BACK TO NORMAL" in console.text
        assert any("NORMAL" in w for w in result.warnings)

    def test_the_845_message_points_at_the_single_step_retry(self):
        # The artefacts do not have to be re-connected: CAL_ADJ's constants
        # are still live, so CAL_STORE alone commits the same calibration.
        message = str(CalibrationSwitchLockout("CAL_STORE", [845]))
        assert "store_step" in message
        assert "do not have to be re-connected" in message

    def test_store_step_is_a_public_single_step_retry(self):
        cal, transport = opened()
        ctx = make_context(cal, commit=True)
        procedure = Fluke5700AArtifactCal()
        # Get the session to the point where CAL_ADJ has completed ...
        run_procedure(procedure, ctx)
        assert ctx.completed("adjust")
        transport.log.clear()
        # ... then commit alone, as the fault-845 message promises.
        result = procedure.store_step(ctx)
        assert mutating(transport) == ["CAL_STORE;*WAI"]
        assert result.committed
        # ... and the retry joins the audit trail rather than vanishing.
        assert ctx.results[-1] is result
        assert procedure.finalize(ctx).committed

    def test_store_step_refuses_after_a_power_cycle(self):
        # CAL_ADJ's constants do not survive one, so a fresh context -- which
        # is what a new session is -- has nothing to commit.
        cal, transport = opened()
        ctx = make_context(cal, commit=True)
        transport.log.clear()
        with pytest.raises(StepOrderError) as exc:
            Fluke5700AArtifactCal().store_step(ctx)
        assert "power cycle" in str(exc.value)
        assert transport.log == []

    def test_the_5725a_switch_is_distinguished(self):
        exc = CalibrationSwitchLockout("CAL_STORE", [850])
        assert "5725A amplifier's OWN" in str(exc)
        assert "5725A" not in str(CalibrationSwitchLockout("CAL_STORE", [845]))

    def test_codes_are_read_from_the_drivers_exception(self):
        class Fault(Exception):
            codes = [(845, "Cal Switch Must Be Enabled")]

        assert proc.fault_codes_of(Fault()) == (845,)

    def test_codes_fall_back_to_the_message(self):
        assert 845 in proc.fault_codes_of(Exception("5700A fault 845: nope"))


class TestNvramUncertainty:
    """The record must never say "not committed" about a command that went
    out and whose answer never came back."""

    def _timeout_after_store(self):
        transport = FakeTransport(default_responses())

        def faults(_cmd):
            if any(c.startswith("CAL_STORE") for c in transport.log):
                # A GPIB read timeout on the drain that follows the EEPROM
                # write -- the write itself almost certainly landed.
                raise TimeoutError("fake GPIB read timeout")
            return "0"

        transport.responses["FAULT?"] = faults
        return Fluke5700A(transport=transport), transport

    def test_a_lost_answer_after_cal_store_is_recorded_as_unknown(self):
        cal, transport = self._timeout_after_store()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, commit=True))

        assert "CAL_STORE;*WAI" in transport.log       # it WAS on the wire
        assert result.failed
        assert result.committed is False               # not *confirmed*
        assert result.nvram_uncertain is True          # ... but not "no"
        assert result.nvram_state is proc.NvramState.UNKNOWN

    def test_the_summary_says_unknown_not_no(self):
        cal, _ = self._timeout_after_store()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, commit=True))
        summary = result.summary()
        assert "committed to NVRAM: UNKNOWN" in summary
        assert "committed to NVRAM: no" not in summary
        assert "CAL_DATE?" in summary                  # how to find out

    def test_the_failed_step_shows_the_command_it_sent(self):
        # describe() rendering "[-]" next to a failed CAL_STORE would say the
        # opposite of what happened.
        cal, _ = self._timeout_after_store()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, commit=True))
        store = next(s for s in result.steps if s.step_id == "store")
        assert store.commands == ("CAL_STORE;*WAI",)
        assert "CAL_STORE;*WAI" in store.describe()
        assert "NVRAM STATE UNKNOWN" in store.describe()
        assert "CAL_DATE?" in " ".join(result.warnings)

    def test_a_guard_refusal_is_not_uncertain(self):
        # StepOrderError and SafetyError are raised before any I/O by
        # construction, so the NVRAM is known-untouched.
        cal, transport = opened()
        ctx = make_context(cal, commit=True)
        procedure = Fluke5700AArtifactCal()
        step = procedure._store_step()
        transport.log.clear()
        with pytest.raises(StepOrderError):
            step.action(ctx, step)
        assert transport.log == []

    def test_a_non_nvram_step_failing_claims_nothing(self):
        # A CAL_REF step that fails commits nothing by construction: only
        # CAL_STORE writes, and it is never reached.
        transport = FakeTransport(default_responses())

        def out(_cmd):
            if any(c.startswith("CAL_TEMP") for c in transport.log):
                return "5.0E+02,V,0"           # front panel moved mid-run
            return "0.0E+00,V,0"

        transport.responses["OUT?"] = out
        cal = Fluke5700A(transport=transport)
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, commit=True))
        assert result.failed
        assert result.nvram_uncertain is False
        assert result.nvram_state is proc.NvramState.UNTOUCHED
        assert "committed to NVRAM: no" in result.summary()


class TestRequiredSteps:
    def test_declining_the_safety_brief_aborts_the_run(self):
        # The brief is the operator's acknowledgement that there is no remote
        # abort and the front-panel STBY key must stay within reach. "No"
        # cannot mean "carry on and commit anyway".
        console = FakeConsole(ok=lambda what: "brief" not in what)
        cal, transport = opened()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, console, commit=True))

        assert mutating(transport) == []
        assert result.failed
        assert "required" in result.error
        assert "cal-temp" in result.unreached
        assert result.committed is False

    @pytest.mark.parametrize("procedure,ctx_kwargs", [
        (Fluke5700ADcZeros(), {}),
        (Fluke5700ACalCheck(), {"ambient_c": 23.5}),
    ])
    def test_every_safety_brief_is_required(self, procedure, ctx_kwargs):
        console = FakeConsole(ok=lambda what: "brief" not in what)
        cal, transport = opened()
        result = run_procedure(procedure,
                               ProcedureContext(device=cal, console=console,
                                                commit=True, **ctx_kwargs))
        assert mutating(transport) == []
        assert result.failed

    def test_an_ordinary_step_may_still_be_declined(self):
        # Only steps marked `required` stop the run; the ref steps rely on the
        # order guard instead, which is a different and better mechanism.
        step = AdjustStep(id="x", prompt="", action=lambda c, s: None)
        assert step.required is False


class TestDryRunAftermath:
    def test_the_operator_is_told_the_new_constants_are_live_but_volatile(self):
        # A dry run really sends CAL_ADJ. The calibrator is now sourcing from
        # constants that will vanish at power-off, and an operator who then
        # uses it to adjust a DMM is using unverified ones.
        console = FakeConsole()
        cal, _ = opened()
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, console))

        assert any("volatile memory" in n for n in result.dry_run_notes)
        assert any("LOST at power-off" in n for n in result.dry_run_notes)
        assert "Power-cycle" in result.summary()
        assert "LOST at power-off" in console.text

    def test_the_zeros_dry_run_says_the_clock_did_not_restart(self):
        cal, _ = opened()
        result = run_procedure(Fluke5700ADcZeros(),
                               ProcedureContext(device=cal,
                                                console=FakeConsole()))
        assert any("has NOT been restarted" in n for n in result.dry_run_notes)

    def test_the_dry_run_quotes_the_prompt_rather_than_issuing_it(self):
        # Sending an operator round the back to flip the CALIBRATION switch
        # and then storing nothing would be a lie told in the right order.
        console = FakeConsole()
        cal, _ = opened()
        run_procedure(Fluke5700AArtifactCal(), make_context(cal, console))
        assert "SKIPPED — dry run. This step would have asked:" in console.text
        assert console.prompted.count("store") == 0


# ══════════════════════════════════════════════════════════════════════
# Safety
# ══════════════════════════════════════════════════════════════════════

class TestSafety:
    def test_preflight_refuses_a_live_output_at_or_above_22_v(self):
        cal, _ = opened(**{"OUT?": "1.0E+03,V,0"})
        with pytest.raises(PreflightError) as exc:
            run_procedure(Fluke5700AArtifactCal(), make_context(cal))
        message = str(exc.value)
        assert "1000" in message
        assert "no remote abort" in message
        assert "STBY" in message

    def test_a_long_term_step_rechecks_the_output(self):
        # Safe at preflight, dangerous by the time the first CAL_REF starts --
        # the front panel can be used at any moment.
        transport = FakeTransport(default_responses())

        def out(_cmd):
            if any(c.startswith("CAL_TEMP") for c in transport.log):
                return "5.0E+02,V,0"
            return "0.0E+00,V,0"

        transport.responses["OUT?"] = out
        cal = Fluke5700A(transport=transport)
        result = run_procedure(Fluke5700AArtifactCal(), make_context(cal))

        assert result.failed
        assert not any("CAL_REF" in cmd for cmd in transport.log)
        assert "SafetyError" in result.steps[-1].error

    def test_an_unreadable_output_is_refused_too(self):
        class Blind:
            def out_actual(self):
                raise RuntimeError("OUT? timed out")

        with pytest.raises(SafetyError) as exc:
            proc.check_output_safe(Blind(), what="CAL_ADJ")
        assert "could not be read back" in str(exc.value)

    def test_22_v_is_the_line(self):
        class Out:
            def __init__(self, v):
                self.v = v

            def out_actual(self):
                return (self.v, "V", 0.0)

        assert proc.check_output_safe(Out(21.9), what="x") == 21.9
        with pytest.raises(SafetyError):
            proc.check_output_safe(Out(22.0), what="x")

    def test_ohms_are_not_volts(self):
        class Ohms:
            def out_actual(self):
                return (10000.0, "OHM", 0.0)

        assert proc.check_output_safe(Ohms(), what="x") == 10000.0

    def test_the_safety_brief_precedes_the_first_long_term_command(self):
        console = FakeConsole()
        cal, _ = opened()
        run_procedure(Fluke5700AArtifactCal(), make_context(cal, console))
        assert "NO REMOTE ABORT" in console.text
        assert "FRONT-PANEL STBY" in console.text

    def test_the_sigint_handler_sets_a_flag_instead_of_raising(self):
        # Raising mid-CAL_ADJ would unwind the driver with an *OPC still
        # outstanding and the calibrator still running, and there is no remote
        # abort to clean up with.
        import signal

        cal, _ = opened()
        ctx = make_context(cal)
        original = signal.getsignal(signal.SIGINT)
        with proc.deferred_sigint(ctx) as _:
            installed = signal.getsignal(signal.SIGINT)
            assert installed is not original
            installed(signal.SIGINT, None)          # as the kernel would
            assert ctx.interrupted is True
            installed(signal.SIGINT, None)          # a second Ctrl-C
        assert signal.getsignal(signal.SIGINT) is original

    def test_sigint_sets_a_flag_and_stops_at_the_step_boundary(self):
        cal, transport = opened()
        console = FakeConsole()
        ctx = make_context(cal, console)

        # Stand in for the signal handler firing during the +10 V step: the
        # poll finishes, the runner stops at the next boundary.
        original = Fluke5700AArtifactCal.cal_ref_step

        def interrupting(self, c, step_id):
            result = original(self, c, step_id)
            c.interrupted = True
            return result

        procedure = Fluke5700AArtifactCal()
        procedure.cal_ref_step = interrupting.__get__(procedure)
        result = run_procedure(procedure, ctx)

        assert result.interrupted
        assert "CAL_REF 1.0000130000E+01 V;*OPC" in transport.log
        assert "CAL_ADJ;*OPC" not in transport.log
        assert "adjust" in result.unreached
        assert not result.complete


class TestPreflight:
    def test_a_cold_calibrator_blocks_a_committing_run(self):
        cal, _ = opened(**{"ONTIME?": "3"})
        with pytest.raises(PreflightError) as exc:
            run_procedure(Fluke5700AArtifactCal(),
                          make_context(cal, commit=True))
        assert "warm-up" in str(exc.value)

    def test_a_cold_calibrator_only_warns_a_dry_run(self):
        # A rehearsal has to be possible at any time.
        cal, _ = opened(**{"ONTIME?": "3"})
        result = run_procedure(Fluke5700AArtifactCal(), make_context(cal))
        assert not result.failed
        assert any("warm-up" in w for w in result.warnings)

    def test_overdue_dc_zeros_block_a_committing_run(self):
        # Same shape as the warm-up gate, and for a stronger reason: the
        # artifact cal writes 2.2 V-range offsets into NVRAM permanently, so
        # stale zeros are baked in and cannot be re-measured away afterwards.
        cal, _ = opened(**{"CAL_DAYS? ZERO": "180"})
        with pytest.raises(PreflightError) as exc:
            run_procedure(Fluke5700AArtifactCal(),
                          make_context(cal, commit=True))
        assert "180 days ago" in str(exc.value)
        assert "dc-zeros" in str(exc.value)

    def test_overdue_dc_zeros_only_warn_a_dry_run(self):
        cal, _ = opened(**{"CAL_DAYS? ZERO": "180"})
        result = run_procedure(Fluke5700AArtifactCal(), make_context(cal))
        assert not result.failed
        assert any("180 days ago" in w for w in result.warnings)

    def test_the_zeros_procedure_is_not_gated_on_overdue_zeros(self):
        # It is the fix; gating it on them would be a deadlock.
        cal, _ = opened(**{"CAL_DAYS? ZERO": "999"})
        result = run_procedure(
            Fluke5700ADcZeros(),
            ProcedureContext(device=cal, console=FakeConsole(), commit=True))
        assert not result.failed, result.error
        assert result.committed

    def test_an_unverifiable_warmup_blocks_a_committing_run(self):
        # "I could not check" must not read as "it is safe" -- the same rule
        # check_output_safe applies to an unreadable OUT?.
        cal, _ = opened(**{"ONTIME?": "not a number"})
        with pytest.raises(PreflightError) as exc:
            run_procedure(Fluke5700AArtifactCal(),
                          make_context(cal, commit=True))
        assert "ONTIME? failed" in str(exc.value)

    def test_an_unverifiable_warmup_only_warns_a_dry_run(self):
        cal, _ = opened(**{"ONTIME?": "not a number"})
        result = run_procedure(Fluke5700AArtifactCal(), make_context(cal))
        assert not result.failed
        assert any("ONTIME? failed" in w for w in result.warnings)

    def test_precheck_runs_before_the_operator_types_anything(self):
        # Three certificates is three certified values, three uncertainties
        # and three serials. Discovering afterwards that the thing on the bus
        # is not a 5700A, and having PreflightError throw the typing away, is
        # the experience precheck exists to prevent.
        console = FakeConsole(answers=[])       # any ask() would blow up
        ctx = ProcedureContext(device=object(), console=console)
        with pytest.raises(PreflightError) as exc:
            run_procedure(Fluke5700AArtifactCal(), ctx)
        assert "5700A" in str(exc.value)
        assert console.asked == []

    def test_missing_standards_block(self):
        cal, _ = opened()
        ctx = ProcedureContext(device=cal, console=FakeConsole(),
                               ambient_c=23.5)
        # prepare() would prompt; call preflight directly.
        problems = Fluke5700AArtifactCal().preflight(ctx)
        assert len(problems) == 3
        assert all("certified value" in p for p in problems)

    def test_a_missing_ambient_blocks(self):
        cal, _ = opened()
        ctx = ProcedureContext(device=cal, console=FakeConsole(),
                               standards=artifact_standards())
        problems = Fluke5700AArtifactCal().preflight(ctx)
        assert any("CAL_TEMP" in p for p in problems)

    def test_a_non_calibrator_object_is_rejected_clearly(self):
        ctx = ProcedureContext(device=object(), console=FakeConsole(),
                               ambient_c=23.5,
                               standards=artifact_standards())
        problems = Fluke5700AArtifactCal().preflight(ctx)
        assert len(problems) == 1
        assert "5700A" in problems[0]

    def test_a_poor_standard_warns_without_blocking(self):
        cal, _ = opened()
        standards = artifact_standards()
        standards["10V"] = make_standard("10V", 10.00013, "20 ppm")
        result = run_procedure(Fluke5700AArtifactCal(),
                               make_context(cal, standards=standards,
                                            commit=True))
        assert not result.failed
        assert any("worse than" in w for w in result.warnings)

    def test_prepare_collects_what_is_missing(self):
        console = FakeConsole(answers=[
            "23.5",
            "10.00013", "1.5 ppm", "732A",
            "9.99987", "4 ppm", "742A-10k",
            "1.00026", "10 ppm", "742A-1",
        ])
        cal, _ = opened()
        ctx = ProcedureContext(device=cal, console=console)
        Fluke5700AArtifactCal().prepare(ctx)
        assert ctx.ambient_c == 23.5
        assert set(ctx.standards) == {"10V", "10KOHM", "1OHM"}
        assert Fluke5700AArtifactCal().preflight(ctx) == []


# ══════════════════════════════════════════════════════════════════════
# The other two procedures, and the registry
# ══════════════════════════════════════════════════════════════════════

class TestDcZeros:
    def test_it_runs_cal_zero_when_committing(self):
        cal, transport = opened()
        result = run_procedure(
            Fluke5700ADcZeros(),
            ProcedureContext(device=cal, console=FakeConsole(), commit=True))
        assert not result.failed, result.error
        assert mutating(transport) == ["CAL_ZERO;*OPC"]
        assert result.committed

    def test_it_needs_no_calibration_switch(self):
        console = FakeConsole()
        cal, _ = opened()
        run_procedure(Fluke5700ADcZeros(),
                      ProcedureContext(device=cal, console=console,
                                       commit=True))
        assert "NOT needed" in console.text

    def test_it_needs_no_standards_or_ambient(self):
        cal, _ = opened()
        ctx = ProcedureContext(device=cal, console=FakeConsole())
        assert Fluke5700ADcZeros().preflight(ctx) == []


class TestCalCheck:
    def test_it_runs_fully_in_a_dry_run_because_it_commits_nothing(self):
        cal, transport = opened()
        result = run_procedure(
            Fluke5700ACalCheck(),
            ProcedureContext(device=cal, console=FakeConsole(),
                             ambient_c=23.5))
        assert not result.failed, result.error
        assert mutating(transport) == ["CAL_TEMP 23.5", "CAL_CHK;*OPC"]
        assert result.committed is False
        assert result.would_commit == ()

    def test_the_previous_check_report_is_captured_first(self):
        cal, transport = opened()
        result = run_procedure(
            Fluke5700ACalCheck(),
            ProcedureContext(device=cal, console=FakeConsole(),
                             ambient_c=23.5))
        assert transport.log.index("CAL_RPT? CHECK") < transport.log.index(
            "CAL_CHK;*OPC")
        assert result.before_report is not None
        assert result.report_diff is not None

    def test_it_needs_an_ambient(self):
        cal, _ = opened()
        ctx = ProcedureContext(device=cal, console=FakeConsole())
        assert any("CAL_TEMP" in p for p in Fluke5700ACalCheck().preflight(ctx))

    def test_a_cold_calibrator_is_refused(self):
        """M15: CAL_CHK always runs with commit=False, so the warm-up gate
        only ever warned -- and a cold check overwrites the CHECK
        performance-history baseline with an out-of-condition point that
        cannot be recovered ([OM ch.6]: check data is replaced by a later
        check).
        """
        cal, transport = opened(**{"ONTIME?": "3"})
        ctx = ProcedureContext(device=cal, console=FakeConsole(),
                               ambient_c=23.5)
        with pytest.raises(PreflightError) as exc:
            run_procedure(Fluke5700ACalCheck(), ctx)
        assert "warm-up" in str(exc.value)
        assert "CAL_CHK;*OPC" not in transport.log

    def test_the_artifact_rehearsal_keeps_its_dry_run_relaxation(self):
        """The one procedure whose dry run has to be possible at any time."""
        cal, _ = opened(**{"ONTIME?": "3"})
        result = run_procedure(Fluke5700AArtifactCal(), make_context(cal))
        assert not result.failed
        assert any("warm-up" in w for w in result.warnings)


class TestMirroredConstants:
    """``cal.procedure`` may not import a driver, so it repeats a handful of
    the driver's constants.  These are the drift guards."""

    def test_calibration_switch_fault_codes(self):
        assert (proc.CALIBRATION_SWITCH_FAULT_CODES
                == f57.CALIBRATION_SWITCH_FAULT_CODES)

    def test_out_of_order_fault_code(self):
        assert (proc.FAULT_CAL_STEPS_OUT_OF_ORDER
                == f57.FAULT_CAL_STEPS_OUT_OF_ORDER)

    def test_warmup_minutes(self):
        assert proc.WARMUP_MINUTES == f57.WARMUP_MAX_MINUTES

    def test_the_cal_temp_specification_band(self):
        assert proc.CAL_TEMP_SPEC_MIN_C == f57.CAL_TEMP_SPEC_MIN_C
        assert proc.CAL_TEMP_SPEC_MAX_C == f57.CAL_TEMP_SPEC_MAX_C

    def test_the_cal_ref_units_are_ones_the_driver_accepts(self):
        assert all(spec.unit in f57.CAL_REF_UNITS
                   for spec in ARTIFACT_STANDARDS)


class TestConsoleSeam:
    """The injected console is the only way this module talks to a human."""

    def test_the_real_operator_console_satisfies_it(self):
        # Signature compatibility, not just method names: the runner calls
        # confirm_ok(what, hint=...) and confirm_explicit(what, hint=...).
        console = OperatorConsole()
        for name in ("show", "prompt", "confirm_ok", "confirm_explicit",
                     "ask"):
            assert callable(getattr(console, name))
        import inspect
        for name in ("confirm_ok", "confirm_explicit"):
            params = inspect.signature(getattr(console, name)).parameters
            assert "hint" in params
            assert params["hint"].kind is inspect.Parameter.KEYWORD_ONLY

    def test_a_console_without_ask_says_so_rather_than_falling_back(self):
        # A fallback to input() would be a second, untested I/O path the
        # injected seam cannot reach.
        class Mute:
            pass

        with pytest.raises(proc.ProcedureError) as exc:
            proc.ask(Mute(), "anything? ")
        assert "no ask() method" in str(exc.value)


class TestRegistry:
    def test_the_5700a_procedures_are_registered(self):
        assert available_procedures()["fluke5700a"] == (
            "artifact-cal", "cal-check", "dc-zeros")

    def test_the_5720a_is_not_advertised(self):
        # The procedures do run on a 5720A (same remote surface), but the
        # registry key is the spec-file id and there is no fluke5720a.yaml:
        # advertising a model id load_model() rejects sends cal_verify
        # operators down a dead end. Reinstate the alias with the spec file.
        assert "fluke5720a" not in available_procedures()

    def test_lookup_is_case_insensitive(self):
        assert get_procedure("FLUKE5700A", "Artifact-Cal").procedure_id == \
            "artifact-cal"

    def test_an_unknown_procedure_lists_what_there_is(self):
        with pytest.raises(proc.ProcedureError) as exc:
            get_procedure("fluke5700a", "nope")
        assert "artifact-cal" in str(exc.value)

    def test_an_unknown_model_says_so(self):
        # A synthetic id, so this cannot break again the way it already has
        # once: ``hp3458a`` used to serve here until ``cal.dmm_procedures``
        # registered an ``external-adjust`` entry for it, and any real model
        # could acquire a procedure the same way.
        with pytest.raises(proc.ProcedureError) as exc:
            get_procedure("no-such-model", "adjust")
        assert "no procedures are registered" in str(exc.value)

    def test_procedures_for_returns_instances(self):
        found = procedures_for("fluke5700a")
        assert len(found) == 3
        assert all(isinstance(p, Procedure) for p in found)

    def test_registering_a_clash_is_refused(self):
        class Impostor:
            model_id = "fluke5700a"
            procedure_id = "artifact-cal"
            name = "not the real one"

        with pytest.raises(proc.ProcedureError):
            proc.register(Impostor())

    def test_registering_the_same_instance_again_is_idempotent(self):
        existing = get_procedure("fluke5700a", "dc-zeros")
        proc.register(existing)
        assert get_procedure("fluke5700a", "dc-zeros") is existing


# ══════════════════════════════════════════════════════════════════════
# The model itself
# ══════════════════════════════════════════════════════════════════════

class TestModel:
    def test_a_result_is_only_ever_produced_by_a_run(self):
        cal, _ = opened()
        result = run_procedure(Fluke5700AArtifactCal(), make_context(cal))
        assert isinstance(result, ProcedureResult)
        assert result.steps                      # every step recorded
        assert [s.step_id for s in result.steps] == [
            "brief", "capture-before", "cal-temp", "ref-10v-pos",
            "ref-10v-neg", "ref-10kohm", "ref-1ohm", "adjust",
            "capture-after", "store"]

    def test_an_nvram_step_gets_a_default_confirmation_text(self):
        step = AdjustStep(id="x", prompt="", action=lambda c, s: None,
                          writes_nvram=True, commands=("BOOM",))
        assert "NON-VOLATILE" in step.confirm
        assert "BOOM" in step.confirm

    def test_step_results_are_immutable_and_normalised(self):
        result = proc.StepResult(step_id="x", outcome=StepOutcome.EXECUTED,
                                 started_at=proc._now(),
                                 finished_at=proc._now(),
                                 commands=["A", "B"], detail={"k": 1})
        assert result.commands == ("A", "B")
        with pytest.raises(TypeError):
            result.detail["k"] = 2

    def test_a_setpoint_knows_what_is_hazardous(self):
        assert proc.SetPoint(1000.0, "V").hazardous
        assert not proc.SetPoint(10.0, "V").hazardous
        assert not proc.SetPoint(10000.0, "OHM").hazardous

    def test_the_summary_says_whether_anything_was_committed(self):
        cal, _ = opened()
        dry = run_procedure(Fluke5700AArtifactCal(), make_context(cal))
        assert "DRY RUN" in dry.summary()
        assert "committed to NVRAM: no" in dry.summary()

        cal2, _ = opened()
        wet = run_procedure(Fluke5700AArtifactCal(),
                            make_context(cal2, commit=True))
        assert "COMMITTING RUN" in wet.summary()
        assert "committed to NVRAM: yes" in wet.summary()

    def test_the_verifier_hook_runs_after_a_step_that_asks_for_one(self):
        from caldmm.cal.plan import PlanPoint
        from caldmm.cal.quantities import Quantity

        point = PlanPoint(quantity=Quantity.DCV, value=10.0)
        seen = []
        step = AdjustStep(id="x", prompt="", verify_after=point,
                          action=lambda c, s: proc.StepResult(
                              step_id=s.id, outcome=StepOutcome.EXECUTED,
                              started_at=proc._now(),
                              finished_at=proc._now()))

        class One:
            model_id = "fake"
            procedure_id = "one"
            name = "one step"

            def preflight(self, ctx):
                return []

            def steps(self, ctx):
                return [step]

            def finalize(self, ctx):
                return proc._finalize(self, ctx)

        ctx = ProcedureContext(device=object(), console=FakeConsole(),
                               verifier=lambda p: seen.append(p) or "ok")
        result = run_procedure(One(), ctx)
        assert seen == [point]
        assert ctx.captures["verify"]["x"] == "ok"
        assert not result.failed


# ══════════════════════════════════════════════════════════════════════
# Import hygiene
# ══════════════════════════════════════════════════════════════════════

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

    import caldmm.cal.procedure as p

    # It takes a driver *object*, so a plain stub is a legal instrument.
    class Stub:
        def cal_temp(self, c): pass
        def cal_ref(self, v, u, **kw): raise AssertionError("must not be sent")
        def cal_adj(self, **kw): pass
        def cal_store(self): pass
        def out_actual(self): return (0.0, "V", 0.0)
        def cal_rpt(self, which="CAL"): return ""
        def ontime(self): return 60
        def cal_days(self, what): return 5

    spec = p.STANDARDS_BY_KEY["10V"]
    std = p.ExternalStandard(spec, 10.00013,
                             p.parse_uncertainty("1.5 ppm", spec, 10.00013))
    assert abs(std.uncertainty_ppm - 1.5) < 1e-9

    ctx = p.ProcedureContext(device=Stub(), console=None, ambient_c=23.0,
                             standards={"10V": std})
    try:
        p.get_procedure("fluke5700a", "artifact-cal").cal_ref_step(
            ctx, "ref-1ohm")
    except p.StepOrderError:
        pass
    else:
        raise AssertionError("out-of-order CAL_REF was not refused")

    leaked = sorted(m for m in sys.modules if m.startswith("caldmm.drivers"))
    assert not leaked, "cal.procedure pulled in drivers: " + repr(leaked)
    print("OK")
    """)


def test_procedure_imports_with_no_hardware_libraries():
    """``cal.procedure`` takes driver objects; it must not import one.

    A subprocess so the root conftest's mock stubs are not in the way.
    """
    completed = subprocess.run([sys.executable, "-c", IMPORT_HYGIENE_SCRIPT],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert "OK" in completed.stdout


# ══════════════════════════════════════════════════════════════════════
# The source seam, and telling the truth about aborting
#
# Added with the HP 34401A adjustment, which is the first procedure whose
# applied values are stepped by a second instrument, and the first whose
# steps the instrument documents a way to stop.
# ══════════════════════════════════════════════════════════════════════

class TestSourceSeam:
    """``ProcedureContext.source`` and the ``needs_source`` declaration."""

    def test_source_defaults_to_none(self):
        """Every procedure written before the 34401A's leaves it unset."""
        ctx = make_context(object())
        assert ctx.source is None

    @staticmethod
    def _registered():
        return [entry for model in available_procedures()
                for entry in procedures_for(model)]


    def test_needs_source_reads_the_declaration(self):
        class Declares:
            needs_source = True

        assert proc.needs_source(Declares()) is True

    def test_require_source_returns_it(self):
        source = object()
        assert make_context(object(), source=source).require_source() is source

    def test_require_source_blames_the_caller_not_the_operator(self):
        """A missing source is a wiring mistake in the app, and says so.

        The operator cannot fix it by doing anything at the bench, so the
        message must not read like an instruction to them.
        """
        with pytest.raises(proc.ProcedureError) as excinfo:
            make_context(object()).require_source()
        text = str(excinfo.value)
        assert "needs_source" in text
        assert "runner" in text

    def test_isinstance_still_accepts_a_procedure_without_needs_source(self):
        """``Procedure`` is runtime_checkable, so every name in its body is a
        ``hasattr`` requirement.  Declaring an *optional* member there would
        silently make it mandatory and fail every existing procedure."""
        assert "needs_source" not in _protocol_members(Procedure)
        found = self._registered()
        assert found, "no procedures registered — the check would be vacuous"
        for entry in found:
            assert isinstance(entry, Procedure), entry.model_id


class TestSetPointFrequency:
    """AC points differ only by frequency, so the setpoint has to carry it."""

    def test_describe_includes_frequency(self):
        assert proc.SetPoint(750.0, "V", freq_hz=900.0).describe() == \
            "750 V at 900 Hz"

    def test_dc_point_is_unchanged(self):
        assert proc.SetPoint(10.0, "V").describe() == "10 V"

    def test_label_still_wins(self):
        assert proc.SetPoint(0.0, "V", label="a four-terminal short",
                             freq_hz=1000.0).describe() == \
            "a four-terminal short"

    def test_frequency_does_not_disturb_the_hazard_line(self):
        assert proc.SetPoint(750.0, "V", freq_hz=50_000.0).hazardous
        assert not proc.SetPoint(0.1, "V", freq_hz=50_000.0).hazardous

    def test_the_two_100v_ac_points_describe_differently(self):
        """The whole reason the field exists: without it the record cannot
        tell the 1 kHz and 50 kHz points of a 34401A adjustment apart."""
        one = proc.SetPoint(100.0, "V", freq_hz=1000.0)
        fifty = proc.SetPoint(100.0, "V", freq_hz=50_000.0)
        assert one.describe() != fifty.describe()


class TestInterruptMessage:
    """What Ctrl-C tells the operator, per step.

    Tested through :func:`interrupt_lines` rather than by raising signals:
    this text is what somebody acts on with their hands near a live terminal,
    so it is worth pinning, and a signal handler is not reachable from a test.
    """

    @staticmethod
    def _ctx(step=None):
        ctx = make_context(object())
        ctx.current_step = step
        return ctx

    @staticmethod
    def _step(**kwargs):
        return AdjustStep(id="s", prompt="p", action=lambda c, s: None,
                          **kwargs)

    def test_default_is_the_5700a_truth(self):
        """No declaration means the calibrator's story, which is the one the
        framework was built around: guide §19.2, no remote abort at all."""
        step = self._step()
        assert step.abortable is False, "abortable must default off"
        text = " ".join(proc.interrupt_lines(self._ctx(step), repeat=False))
        assert "cannot be aborted remotely" in text

    def test_no_current_step_is_also_the_default(self):
        text = " ".join(proc.interrupt_lines(self._ctx(), repeat=False))
        assert "cannot be aborted remotely" in text

    def test_abortable_step_does_not_claim_there_is_no_abort(self):
        """The regression this exists to prevent.

        Telling a 34401A operator that a calibration cannot be stopped is
        false (Service Guide p.76) and dangerous: the next thing to reach for
        is the line switch, and p.77 says that can lose ALL calibration
        memory during a zero adjustment.
        """
        ctx = self._ctx(self._step(abortable=True))
        text = " ".join(proc.interrupt_lines(ctx, repeat=False))
        assert "cannot be aborted remotely" not in text
        assert "CAN be stopped at the instrument" in text

    def test_abortable_step_says_the_run_will_not_do_it(self):
        """``abortable`` is a declaration, not a mechanism.  Nothing here
        sends a device clear, and the message must not imply otherwise."""
        ctx = self._ctx(self._step(abortable=True))
        text = " ".join(proc.interrupt_lines(ctx, repeat=False))
        assert "will not do it for you" in text

    def test_abort_hint_is_shown(self):
        ctx = self._ctx(self._step(
            abortable=True,
            abort_hint="press any front-panel key except Shift (p.76)"))
        text = " ".join(proc.interrupt_lines(ctx, repeat=False))
        assert "front-panel key except Shift" in text

    def test_repeat_differs_from_the_first_press(self):
        first = proc.interrupt_lines(self._ctx(), repeat=False)
        again = proc.interrupt_lines(self._ctx(), repeat=True)
        assert first != again
        assert "already stopping" in " ".join(again)

    def test_repeat_on_an_abortable_step_still_does_not_lie(self):
        ctx = self._ctx(self._step(abortable=True))
        text = " ".join(proc.interrupt_lines(ctx, repeat=True))
        assert "no remote abort" not in text

    def test_the_repeat_message_never_names_line_power(self):
        """M42: the framework's default message is the 5700A's truth.

        On a 3458A there is no STBY key, and cutting line power mid-CAL/ACAL
        invalidates the autocal constants (Calibration Manual p.87: "If power
        is turned off ... you must perform an ACAL ALL").  The howto has to
        apologise for this sentence; it should not exist.
        """
        text = " ".join(proc.interrupt_lines(self._ctx(), repeat=True))
        assert "line power" not in text
        assert "do not" in text.lower() and "power" in text.lower()

    def test_a_non_abortable_step_may_still_carry_a_hint(self):
        """The 5700A's STBY key belongs in the 5700A's own hint, not in the
        message every instrument gets."""
        ctx = self._ctx(self._step(
            abort_hint="the front-panel STBY key is the only stop"))
        text = " ".join(proc.interrupt_lines(ctx, repeat=False))
        assert "front-panel STBY key" in text
        assert "cannot be aborted remotely" in text

    def test_the_5700a_long_term_steps_carry_that_hint(self):
        """Where the sentence really is true, it is still said."""
        cal, _ = opened()
        ctx = make_context(cal)
        steps = {s.id: s for s in Fluke5700AArtifactCal().steps(ctx)}
        for step_id in ("ref-10v-pos", "adjust"):
            assert "STBY key" in steps[step_id].abort_hint, step_id
        zeros = {s.id: s for s in Fluke5700ADcZeros().steps(ctx)}
        assert "STBY key" in zeros["zeros"].abort_hint


class TestCurrentStepTracking:
    """The runner has to publish the step in flight, or the message above
    can never be anything but the default."""

    def test_runner_sets_and_clears_current_step(self):
        seen = []

        class OneStep:
            model_id = "fake"
            procedure_id = "fake"
            name = "fake"

            def preflight(self, ctx):
                return []

            def steps(self, ctx):
                def action(c, s):
                    seen.append(c.current_step)
                    return proc.StepResult(
                        step_id=s.id, outcome=StepOutcome.EXECUTED,
                        started_at=None, finished_at=None)
                yield AdjustStep(id="only", prompt="go", action=action,
                                 requires_operator=False, abortable=True)

            def finalize(self, ctx):
                return ProcedureResult(
                    model_id="fake", procedure_id="fake", name="fake",
                    started_at=ctx.started_at, finished_at=ctx.finished_at,
                    dry_run=ctx.dry_run, steps=tuple(ctx.results))

        ctx = make_context(object())
        run_procedure(OneStep(), ctx)
        assert seen and seen[0].id == "only"
        assert seen[0].abortable is True
        # cleared once the run is over, so a later Ctrl-C outside any step
        # falls back to the conservative default
        assert ctx.current_step is None


class TestNvramVerifyVocabulary:
    """What an operator is told to send after an ``nvram_uncertain`` result.

    The framework was written for the 5700A and named ``CAL_DATE?`` /
    ``CAL_RPT?`` on every path, including the two meters that have neither.
    That message is the only thing standing between an operator and an
    instrument of unknown calibration state, so it has to name a command the
    instrument actually has.
    """


    def test_an_unknown_model_gets_a_description_not_a_guess(self):
        """A wrong command is worse than an honest 'look this up'."""
        hint = proc.nvram_verify_hint("some-future-meter")
        assert "?" not in hint
        assert "calibration-count" in hint

    def test_case_and_whitespace_do_not_matter(self):
        assert proc.nvram_verify_hint("  HP3458A ") == "CALNUM? / CALSTR?"

    def test_every_registered_model_has_an_entry(self):
        """A model with a procedure but no entry would silently fall back to
        the generic text at the worst possible moment."""
        for model in available_procedures():
            assert model in proc.NVRAM_VERIFY_QUERIES, model


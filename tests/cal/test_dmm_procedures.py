"""Tests for the DMM external-adjustment procedures.

Offline only.  The instrument is a **real** :class:`caldmm.drivers.hp3458a.
HP3458A` driving a fake GPIB transport, so every assertion about "what went on
the wire" is about what the driver really emits -- not about what a mock was
told to record.  The operator is a fake console with scripted answers.

Fixtures are built through the real constructors on purpose: an
:class:`~caldmm.cal.procedure.ExternalStandard` validates in
``__post_init__``, so a test cannot fabricate a standard the operator interface
would have refused, and a :class:`~caldmm.cal.procedure.ProcedureResult` is
only ever produced by :func:`~caldmm.cal.procedure.run_procedure` or the
procedure's own ``finalize``.
"""

import subprocess
import sys
import textwrap
from datetime import datetime, timezone

import pytest
from unittest.mock import patch

from caldmm.cal import dmm_procedures as dp
from caldmm.cal.dmm_procedures import (
    ACAL_AC_SECONDS,
    ADJUSTMENT_ORDER,
    AMBIENT_MAX_C,
    AMBIENT_MIN_C,
    GAIN_CHECK_FRACTION,
    HP3458A_STANDARDS,
    HP3458AExternalAdjust,
    NOT_IMPLEMENTED_NOTES,
    OHMS_DELAY_LONG_S,
    OHMS_DELAY_SHORT_S,
    PROCEDURE_ID,
    ProcedureNotImplementedError,
    SECURITY_CODE_RE,
    SHORT_MAX_V,
    SOURCED_POINT_10KOHM,
    SOURCED_POINT_10V,
    STANDARD_10KOHM,
    STANDARD_10V,
    WiringError,
    parse_ambient_c,
    parse_hours,
    parse_security_code,
    require_in_band,
    require_valid_security_code,
    require_write_enabled,
    sourced_envelope_problems,
)
from caldmm.cal.procedure import (
    ExternalStandard,
    NvramState,
    PreflightError,
    ProcedureContext,
    ProcedureError,
    SafetyError,
    StandardValueError,
    StepOrderError,
    StepOutcome,
    StepResult,
    UNSENT_GUARDS,
    available_procedures,
    classify_failure,
    get_procedure,
    needs_source,
    run_procedure,
    source_policy,
)
from caldmm.cal.quantities import Connection, Quantity
from caldmm.cal.roles import SourceHealth, SourceState, Uncertainty
from caldmm.drivers.hp3458a import (
    AUXERR_CAL_RAM_WRITE_PROTECTION,
    ERR_CALIBRATION,
    ERR_HARDWARE,
    HP3458A,
    CalibrationLockoutError,
    CalibrationWriteNotAllowed,
)

SECRET = "3458"

#: Certified values close enough to nominal to pass ``ExternalStandard``'s
#: decade gate, and far enough from it to be visibly not the nominal.
CERT_10V = 10.0000123
CERT_10KOHM = 10000.003


# ── the operator ──────────────────────────────────────────────────────

class FakeConsole:
    """An operator with scripted answers, and a transcript.

    Signature-compatible with :class:`caldmm.lib.console.OperatorConsole`.
    ``ok`` and ``explicit`` may be a bool or a callable taking the prompt text,
    which is how a test declines exactly one step.
    """

    def __init__(self, answers=None, *, ok=True, explicit=True):
        self.answers = list(answers or [])
        self.shown = []
        self.prompted = []
        self.asked = []
        self.confirm_ok_calls = []
        self.confirm_explicit_calls = []
        self._ok = ok
        self._explicit = explicit

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
        if isinstance(self._explicit, BaseException):
            raise self._explicit
        return (self._explicit(what) if callable(self._explicit)
                else bool(self._explicit))

    def ask(self, question):
        self.asked.append(question)
        if not self.answers:
            raise AssertionError(f"console asked an unscripted question: "
                                 f"{question!r}")
        return self.answers.pop(0)

    @property
    def text(self):
        return "\n".join(" | ".join(str(p) for p in entry)
                         for entry in self.shown)


# ── the instrument ────────────────────────────────────────────────────

class FakeGpib:
    """A real fake for ``Gpib.Gpib`` that answers the queries this uses.

    Routes by command rather than by call order, so a test asserts the
    *sequence the driver emitted*.  ``read()`` returns bytes, as linux-gpib
    does.  ``serial_poll`` reports "data available" only when a *reading* is
    queued, which is what ``read_single_sample`` polls on.
    """

    IDN = "HP3458A"

    def __init__(self):
        self.written = []
        self.err = 0
        self.auxerr = 0
        #: Seeded into the registers *by* a CAL / ACAL, so the driver's
        #: pre-clear does not swallow them.
        self.err_on_cal = 0
        self.auxerr_on_cal = 0
        self.err_on_acal = 0
        self.auxerr_on_acal = 0
        self.calnum = 42
        self.cal_string = "CALIBRATED 2026-01-01"
        self.temp_c = 24.7
        #: What a reading returns, chosen by the selected function and range
        #: exactly as a real meter's would be: the 100 mV range sees the
        #: four-terminal short, the 10 V range sees the dc standard.
        self.short_v = 2.0e-6
        self.standard_v = CERT_10V
        #: A queue, so the p.44 settling test can be given two answers.  The
        #: last entry repeats.
        self.ohms_readings = [CERT_10KOHM]
        #: What the meter sees while a §8 safe-level probe is on the
        #: terminals.  Consumed by the reading that follows it, so the canned
        #: per-range values still answer every real point.
        self.probe_reading = None
        self.cal_commands = []
        self._func = "DCV"
        self._range = 0.0
        self._pending = []          # list of (bytes, is_reading)

    # -- Gpib.Gpib surface ----------------------------------------------
    def write(self, cmd):
        self.written.append(cmd)
        self._dispatch(cmd.strip())

    def read(self):
        if not self._pending:
            return b"0\r\n"
        payload, _ = self._pending.pop(0)
        return payload

    def clear(self):
        pass

    def serial_poll(self):
        return 0x80 if any(is_r for _, is_r in self._pending) else 0x00

    # -- instrument emulation -------------------------------------------
    def _queue(self, text, reading=False):
        self._pending.append((f"{text}\r\n".encode(), reading))

    def _next_reading(self):
        if self.probe_reading is not None:
            value, self.probe_reading = self.probe_reading, None
            return value
        if self._func == "OHMF":
            queue = self.ohms_readings
            return queue.pop(0) if len(queue) > 1 else queue[0]
        return self.short_v if self._range <= 0.2 else self.standard_v

    def _select(self, cmd, func):
        self._func = func
        try:
            self._range = float(cmd.split(None, 1)[1])
        except (IndexError, ValueError):                # pragma: no cover
            self._range = 0.0

    def _dispatch(self, cmd):
        if cmd == "ERR?":
            self._queue(self.err)
            self.err = 0                     # ERR? reads *and clears*
        elif cmd == "AUXERR?":
            self._queue(self.auxerr)
            self.auxerr = 0
        elif cmd == "CALNUM?":
            self._queue(self.calnum)
        elif cmd == "CALSTR?":
            self._queue(self.cal_string)
        elif cmd == "TEMP?":
            self._queue(self.temp_c)
        elif cmd == "ID?":
            self._queue(self.IDN)
        elif cmd.startswith("DCV "):
            self._select(cmd, "DCV")
        elif cmd.startswith("OHMF "):
            self._select(cmd, "OHMF")
        elif cmd.startswith("TARM SGL"):
            self._queue(repr(self._next_reading()), reading=True)
        elif cmd.startswith("CAL ") and not cmd.endswith("?"):
            self.cal_commands.append(cmd)
            self.calnum += 1
            self.err |= self.err_on_cal
            self.auxerr |= self.auxerr_on_cal
        elif cmd.startswith("ACAL "):
            self.cal_commands.append(cmd)
            self.err |= self.err_on_acal
            self.auxerr |= self.auxerr_on_acal

    @property
    def commands(self):
        return [c.strip() for c in self.written]


@pytest.fixture
def bench(monkeypatch):
    """A fake 3458A on the bus, with every sleep in the path removed."""
    fake = FakeGpib()
    monkeypatch.setattr("caldmm.drivers.hp3458a.time.sleep",
                        lambda seconds: None)
    monkeypatch.setattr("caldmm.cal.dmm_procedures.time.sleep",
                        lambda seconds: None)
    with patch("caldmm.drivers.hp3458a.Gpib.Gpib", return_value=fake):
        yield fake


def meter(bench, *, allow_write=True, **kwargs):
    dev = HP3458A(allow_calibration_write=allow_write, **kwargs)
    bench.written.clear()
    return dev


def standards():
    """The two artefacts, built through the validating constructor."""
    return {
        "10V": ExternalStandard(spec=STANDARD_10V, certified=CERT_10V,
                                uncertainty=CERT_10V * 1.5e-6,
                                serial="732B-001"),
        "10KOHM": ExternalStandard(spec=STANDARD_10KOHM,
                                   certified=CERT_10KOHM,
                                   uncertainty=CERT_10KOHM * 4.0e-6,
                                   serial="742A-002"),
    }


def context(bench, *, commit=False, console=None, allow_write=None,
            security_code=SECRET, ambient_c=22.5, acal_age_h=2.0, **kwargs):
    """A fully prepared context: no prompt is needed to run the sequence."""
    if allow_write is None:
        allow_write = commit
    dev = meter(bench, allow_write=allow_write, **kwargs)
    ctx = ProcedureContext(device=dev,
                           console=console or FakeConsole(),
                           commit=commit, ambient_c=ambient_c,
                           standards=standards(), operator="tester")
    ctx.acal_age_h = acal_age_h
    ctx.security_code = security_code
    return ctx


def _stamp():
    return datetime.now(timezone.utc)


def _last_delay_before(commands, marker):
    """The ``DELAY`` in force when *marker* went out."""
    stop = commands.index(marker)
    delays = [c for c in commands[:stop] if c.startswith("DELAY ")]
    return delays[-1]


PROC = HP3458AExternalAdjust()

CAL_ZERO = "CAL 0"
CAL_GAIN = f"CAL {CERT_10V:.10G}"
CAL_OHMS = f"CAL {CERT_10KOHM:.10G}"


# ── registration ──────────────────────────────────────────────────────

class TestRegistration:
    """``--adjust`` finds a procedure for both meters, by model id."""


    def test_the_name_keeps_the_cal_acal_distinction_loud(self):
        assert "not ACAL" in HP3458AExternalAdjust.name
        assert PROCEDURE_ID == "external-adjust"


# ── operator entry ────────────────────────────────────────────────────

class TestAmbientBand:
    """15-30 C is the 3458A's band, and it is not the 5700A's."""

    @pytest.mark.parametrize("text,expected", [
        ("22", 22.0), ("22.5 C", 22.5), ("18 degC", 18.0), ("30 °C", 30.0),
        ("15", 15.0)])
    def test_accepted(self, text, expected):
        assert parse_ambient_c(text) == pytest.approx(expected)

    @pytest.mark.parametrize("text", ["14.9", "31", "33", "-5"])
    def test_outside_the_band_is_refused(self, text):
        with pytest.raises(StandardValueError, match="15-30"):
            parse_ambient_c(text)

    def test_the_band_is_tighter_than_the_5700as(self):
        # 33 C is legal for a 5700A artifact calibration and not for this.
        from caldmm.cal.procedure import parse_temperature
        assert parse_temperature("33") == 33.0
        with pytest.raises(StandardValueError):
            parse_ambient_c("33")
        assert (AMBIENT_MIN_C, AMBIENT_MAX_C) == (15.0, 30.0)

    def test_a_non_temperature_unit_is_refused(self):
        with pytest.raises(StandardValueError, match="give °C"):
            parse_ambient_c("22 F")


class TestElapsedHours:
    @pytest.mark.parametrize("text,expected", [
        ("3", 3.0), ("3 h", 3.0), ("90 min", 1.5), ("2 d", 48.0)])
    def test_units(self, text, expected):
        assert parse_hours(text) == pytest.approx(expected)

    def test_blank_is_unknown(self):
        assert parse_hours("   ") is None

    def test_a_wrong_unit_is_refused(self):
        with pytest.raises(StandardValueError, match="hours, minutes or days"):
            parse_hours("3 volts")


class TestStandardsRejectDecadeErrors:
    """The gate that catches the mistake the meter would happily accept.

    ``CAL 1.0`` and ``CAL 1E3`` are both *legal* arguments per Calibration
    Manual p.88, so nothing downstream would refuse them.
    """

    def test_one_volt_for_a_ten_volt_standard(self):
        with pytest.raises(StandardValueError, match="decade or units"):
            ExternalStandard(spec=STANDARD_10V, certified=1.0,
                             uncertainty=1.5e-6)

    def test_one_kilohm_for_a_ten_kilohm_standard(self):
        with pytest.raises(StandardValueError, match="decade or units"):
            ExternalStandard(spec=STANDARD_10KOHM, certified=1.0e3,
                             uncertainty=4.0e-3)

    def test_an_uncertainty_is_mandatory(self):
        with pytest.raises(StandardValueError, match="uncertainty is required"):
            ExternalStandard(spec=STANDARD_10V, certified=CERT_10V,
                             uncertainty=0.0)

    def test_both_artefacts_chapter_3_requires_are_declared(self):
        # Calibration Manual p.37 "Required Equipment": a 10 V dc standard and
        # a 10 kohm resistance standard.  The low-thermal short is equipment,
        # not a certified artefact, so it has no ExternalStandard.
        assert HP3458A_STANDARDS == (STANDARD_10V, STANDARD_10KOHM)

    def test_the_resistance_standard_is_declared_in_ohms(self):
        # CAL takes ohms (p.89's example is CAL 10000.001), so the spec is in
        # ohms and a certificate written in kohm is scaled at the prompt.
        assert STANDARD_10KOHM.unit == "OHM"
        assert STANDARD_10KOHM.nominal == 10000.0


# ── preflight ─────────────────────────────────────────────────────────

class TestPreflight:
    def test_a_prepared_context_has_no_problems(self, bench):
        ctx = context(bench)
        assert PROC.preflight(ctx) == []

    def test_a_non_3458a_is_refused(self, bench):
        bench.IDN = "HEWLETT-PACKARD,34401A,0,10-5-2"
        ctx = context(bench)
        problems = PROC.preflight(ctx)
        assert any("not a 3458A" in p for p in problems)

    def test_an_object_without_the_driver_methods_is_refused(self):
        ctx = ProcedureContext(device=object(), console=FakeConsole())
        problems = PROC.preflight(ctx)
        assert len(problems) == 1
        assert "does not implement" in problems[0]
        assert "adjust" in problems[0]

    def test_a_committing_run_without_the_write_opt_in_is_refused(self, bench):
        ctx = context(bench, commit=True, allow_write=False)
        problems = PROC.preflight(ctx)
        assert any("allow_calibration_write=True" in p for p in problems)

    def test_a_missing_standard_is_refused(self, bench):
        ctx = context(bench)
        del ctx.standards["10KOHM"]
        assert any("10 kΩ" in p for p in PROC.preflight(ctx))

    def test_a_missing_ambient_is_refused(self, bench):
        ctx = context(bench, ambient_c=None)
        assert any("no ambient temperature" in p for p in PROC.preflight(ctx))

    def test_a_stale_acal_warns_and_does_not_block(self, bench):
        ctx = context(bench, acal_age_h=200.0)
        assert PROC.preflight(ctx) == []
        assert any("200 h ago" in w for w in ctx.warnings)
        assert any("does not block" in w for w in ctx.warnings)

    def test_an_unknown_acal_age_warns(self, bench):
        ctx = context(bench, acal_age_h=None)
        assert PROC.preflight(ctx) == []
        assert any("time since the last ACAL is unknown" in w
                   for w in ctx.warnings)

    def test_a_poor_standard_uncertainty_warns(self, bench):
        ctx = context(bench)
        ctx.standards["10V"] = ExternalStandard(
            spec=STANDARD_10V, certified=CERT_10V,
            uncertainty=CERT_10V * 20e-6)
        assert PROC.preflight(ctx) == []
        assert any("worse than the ±1.5 ppm" in w for w in ctx.warnings)

    def test_a_freerunning_meter_is_refused(self, bench):
        ctx = context(bench)
        ctx.device.stop()
        ctx.device.freerunning = True
        assert any("free-running" in p for p in PROC.preflight(ctx))

    def test_preflight_failure_stops_the_run_before_step_one(self, bench):
        # A model mismatch: precheck catches it *before* prepare, so the
        # operator is not asked for two certificates first.
        bench.IDN = "HEWLETT-PACKARD,34401A,0,10-5-2"
        ctx = context(bench, commit=True)
        bench.written.clear()
        with pytest.raises(PreflightError, match="not a 3458A"):
            run_procedure(PROC, ctx)
        assert not any(c.startswith("CAL ") for c in bench.commands)
        assert ctx.console.asked == []


# ── the mandatory order ───────────────────────────────────────────────

class TestOrderIsEnforcedBeforeTheWire:
    """Chapter 3: 'You must perform the adjustments in the order presented'."""

    def test_the_order_matches_the_manual(self):
        assert [step for step, _ in ADJUSTMENT_ORDER] == [
            "offset-front", "offset-rear", "dc-gain", "resistance", "acal-ac"]
        # p.43: the resistance adjustment relies on the dc gain adjustment.
        assert "dc-gain" in dict(ADJUSTMENT_ORDER)["resistance"]

    @pytest.mark.parametrize("step_id", [
        "offset-rear", "dc-gain", "resistance", "acal-ac"])
    def test_a_step_with_unmet_prerequisites_is_refused(self, bench, step_id):
        ctx = context(bench, commit=True)
        with pytest.raises(StepOrderError, match="did not complete"):
            PROC.require_order(ctx, step_id)

    def test_the_guard_type_is_one_classify_failure_calls_unsent(self):
        assert StepOrderError in UNSENT_GUARDS
        assert SafetyError in UNSENT_GUARDS

    def test_classify_failure_files_an_order_error_as_never_sent(self):
        nvram, sent, skipped = classify_failure(
            StepOrderError("no"), ("CAL 0",), writes_nvram=True)
        assert nvram is NvramState.UNTOUCHED
        assert sent == () and skipped == ("CAL 0",)

    def test_a_declined_step_blocks_everything_downstream_of_it(self, bench):
        """Decline the REAR offset; the dc gain adjustment must refuse."""
        console = FakeConsole(
            explicit=lambda what: "REAR terminal offset" not in what)
        ctx = context(bench, commit=True, console=console)
        result = run_procedure(PROC, ctx)

        rear = result.steps[[s.step_id for s in result.steps]
                            .index("offset-rear")]
        assert rear.outcome is StepOutcome.SKIPPED_DECLINED
        gain = result.steps[[s.step_id for s in result.steps]
                            .index("dc-gain")]
        assert gain.outcome is StepOutcome.FAILED
        assert "offset-rear" in gain.error
        # NOTHING went on the wire for the refused step.
        assert gain.commands == ()
        assert gain.skipped_commands == (CAL_GAIN,)
        assert gain.nvram is NvramState.UNTOUCHED
        assert CAL_GAIN not in bench.cal_commands
        assert bench.cal_commands == [f"CAL 0,{SECRET}"]   # FRONT only

    def test_a_step_cannot_be_taken_twice(self, bench):
        ctx = context(bench, commit=True)
        run_procedure(PROC, ctx)
        with pytest.raises(StepOrderError, match="twice"):
            PROC.require_order(ctx, "offset-front")

    def test_an_unknown_step_is_a_programming_error(self, bench):
        ctx = context(bench)
        with pytest.raises(ProcedureError, match="not an ordered CAL step"):
            PROC.require_order(ctx, "brief")


# ── the value-band guard ──────────────────────────────────────────────

class TestValueBandGuard:
    """Mirrored from the driver so the refusal is classified as unsent.

    ``hp3458a.adjustment_kind`` raises a plain ``ValueError`` before
    transmission, which ``classify_failure`` would file as "sent".
    """

    @pytest.mark.parametrize("value,kind", [
        (0.0, "offset"), (1.0, "dc_gain"), (10.0, "dc_gain"),
        (12.0, "dc_gain"), (1.0e3, "resistance"), (10.0e3, "resistance"),
        (12.0e3, "resistance")])
    def test_documented_bands(self, value, kind):
        assert require_in_band(value, "CAL") == kind

    @pytest.mark.parametrize("value", [0.5, 12.5, 100.0, 12001.0, -10.0])
    def test_outside_every_band_raises_a_listed_guard(self, value):
        with pytest.raises(SafetyError, match="outside every documented"):
            require_in_band(value, "CAL")

    def test_a_non_finite_value_is_refused(self):
        with pytest.raises(SafetyError):
            require_in_band(float("nan"), "CAL")


class TestTheDriverGateCannotBeBypassed:
    """A procedure cannot get round task E2's explicit opt-in."""

    def test_the_procedure_refuses_first_with_a_listed_guard(self, bench):
        ctx = context(bench, commit=True, allow_write=False)
        bench.written.clear()
        with pytest.raises(SafetyError, match="allow_calibration_write"):
            require_write_enabled(ctx, "CAL 0")
        assert bench.commands == []

    def test_the_driver_still_refuses_if_the_guard_is_removed(self, bench):
        """Belt and braces: the driver's own gate is independent."""
        dev = meter(bench, allow_write=False)
        with pytest.raises(CalibrationWriteNotAllowed):
            dev.adjust(0.0)
        assert bench.commands == []

    def test_a_committing_run_without_the_opt_in_never_reaches_the_wire(
            self, bench):
        ctx = context(bench, commit=True, allow_write=False)
        bench.written.clear()
        # Preflight blocks it, so the run never starts at all.
        with pytest.raises(PreflightError, match="allow_calibration_write"):
            run_procedure(PROC, ctx)
        assert bench.cal_commands == []

    def test_a_dry_run_is_opened_without_the_opt_in_and_still_rehearses(
            self, bench):
        ctx = context(bench, commit=False, allow_write=False)
        result = run_procedure(PROC, ctx)
        assert result.dry_run
        assert bench.cal_commands == []
        assert result.would_commit


# ── the dry run ───────────────────────────────────────────────────────

class TestDryRunIsCompleteButCommitsNothing:
    def test_no_cal_reaches_the_wire(self, bench):
        ctx = context(bench, commit=False)
        run_procedure(PROC, ctx)
        assert bench.cal_commands == []
        assert not any(c.startswith("CAL ") or c.startswith("ACAL ")
                       for c in bench.commands)

    def test_every_write_is_logged_as_skipped_with_its_exact_command(
            self, bench):
        ctx = context(bench, commit=False)
        result = run_procedure(PROC, ctx)
        assert result.would_commit == (
            CAL_ZERO, CAL_ZERO, CAL_GAIN, CAL_OHMS, "ACAL AC")
        for step in result.steps:
            if step.writes_nvram:
                assert step.outcome is StepOutcome.SKIPPED_DRY_RUN
                assert step.commands == ()
                assert step.nvram is NvramState.UNTOUCHED

    def test_the_non_committing_steps_really_run(self, bench):
        ctx = context(bench, commit=False)
        run_procedure(PROC, ctx)
        commands = bench.commands
        # The manual's setup: DCV on the 100 mV range for the shorts (p.38),
        # the 10 V range for the standard (p.42), OCOMP ON and 4-wire ohms
        # for the resistance standard (p.44).
        assert "DCV 1.00E-01" in commands
        assert f"DCV {CERT_10V:.2E}" in commands
        assert "OCOMP ON" in commands
        assert f"OHMF {CERT_10KOHM:.2E}" in commands
        # two shorts, the 10 V standard, and the two-delay ohms settling
        # test the p.44 note asks for.
        assert commands.count("TARM SGL,1") == 5
        assert "CALNUM?" in commands and "TEMP?" in commands

    def test_it_says_the_meter_was_reconfigured_even_though_nothing_was_written(
            self, bench):
        ctx = context(bench, commit=False)
        result = run_procedure(PROC, ctx)
        assert any("function, range, OCOMP and DELAY" in w
                   for w in result.warnings)

    def test_a_dry_run_is_never_committed(self, bench):
        ctx = context(bench, commit=False)
        result = run_procedure(PROC, ctx)
        assert result.committed is False
        assert result.nvram_uncertain is False
        assert result.nvram_state is NvramState.UNTOUCHED
        # And it is not "failed" either -- which is exactly why nothing may
        # branch on ``failed``.
        assert result.failed is False


# ── the committing run ────────────────────────────────────────────────

class TestCommittingRun:
    def test_the_documented_sequence_in_the_documented_order(self, bench):
        ctx = context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        assert bench.cal_commands == [
            f"CAL 0,{SECRET}",                 # p.40 front terminal offset
            f"CAL 0,{SECRET}",                 # p.41 rear terminal offset
            f"{CAL_GAIN},{SECRET}",            # p.42 dc gain
            f"{CAL_OHMS},{SECRET}",            # p.45 resistance and dc current
            f"ACAL AC,{SECRET}",               # p.45 step 7
        ]
        assert result.committed is True
        assert result.complete is True
        assert result.nvram_state is NvramState.WRITTEN

    def test_every_write_needs_its_own_typed_confirmation(self, bench):
        console = FakeConsole()
        ctx = context(bench, commit=True, console=console)
        run_procedure(PROC, ctx)
        assert len(console.confirm_explicit_calls) == 5
        assert all("NON-VOLATILE" in what or "continuous memory" in what
                   for what in console.confirm_explicit_calls)

    def test_declining_the_confirmation_stores_nothing(self, bench):
        console = FakeConsole(explicit=False)
        ctx = context(bench, commit=True, console=console)
        result = run_procedure(PROC, ctx)
        assert bench.cal_commands == []
        assert result.committed is False
        # The trap this whole subsystem exists to avoid: a declined commit is
        # not a failure, and must never read as a success.
        assert result.failed is False
        assert result.complete is False
        assert result.would_commit == (
            CAL_ZERO, CAL_ZERO, CAL_GAIN, CAL_OHMS, "ACAL AC")

    def test_the_calnum_before_and_after_are_both_recorded(self, bench):
        ctx = context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        captures = result.detail["captures"]
        assert captures["before"]["calnum"] == 42
        assert captures["after"]["calnum"] == 46      # four CALs
        assert captures["after"]["temp_c"] == pytest.approx(24.7)

    def test_calnum_not_advancing_on_a_committing_run_fails_the_step(self, bench):
        """A meter that accepts CAL and stores nothing must not read as success.

        This used to be caught only here, as a session-level warning. The
        driver's ``adjust()`` now compares ``CALNUM?`` across each individual
        ``CAL`` and raises ``CalibrationLockoutError``, so the very first
        adjustment step fails and the run stops — earlier, and per point
        rather than per session. The session-level comparison in
        ``_capture_after_step`` is kept as a backstop for the cross-step case.
        """
        ctx = context(bench, commit=True)
        original = bench._dispatch

        def frozen(cmd):
            before = bench.calnum
            original(cmd)
            bench.calnum = before
        bench._dispatch = frozen
        result = run_procedure(PROC, ctx)

        assert result.failed is True
        assert result.complete is False
        failures = [s for s in result.steps if s.error]
        assert failures, "the stalled counter must fail a step"
        assert any("did not advance" in s.error for s in failures)
        # The operator is told about the two things that could have refused it.
        assert any("J132" in s.error for s in failures)

    def test_the_acal_wait_is_the_manuals_figure(self, bench, monkeypatch):
        """The step reads ERR? the moment the wait is over.

        Waiting short would query a meter that is still autocalibrating.  The
        driver's enum used to say 140 s against p.87's 165 s; it is now
        correct, and this pins the two together so they cannot drift apart
        again.
        """
        from caldmm.drivers.hp3458a import ACAL
        assert ACAL_AC_SECONDS == 165
        assert int(ACAL.AC.duration) == ACAL_AC_SECONDS
        slept = []
        monkeypatch.setattr("caldmm.cal.dmm_procedures.time.sleep",
                            slept.append)
        ctx = context(bench, commit=True)
        run_procedure(PROC, ctx)
        # ``time`` is one module object, so the driver's sleeps land here
        # too; what matters is that the ACAL wait is the manual's figure.
        assert ACAL_AC_SECONDS in slept

    def test_the_ohms_settling_test_from_the_p44_note(self, bench):
        ctx = context(bench, commit=True)
        run_procedure(PROC, ctx)
        commands = bench.commands
        assert f"DELAY {OHMS_DELAY_SHORT_S:.2E}" in commands
        assert f"DELAY {OHMS_DELAY_LONG_S:.2E}" in commands
        # Agreeing readings -> the short delay is restored for the adjustment.
        assert _last_delay_before(commands, f"{CAL_OHMS},{SECRET}") == \
            f"DELAY {OHMS_DELAY_SHORT_S:.2E}"

    def test_the_closing_delay_is_recorded_even_though_it_is_not_declared(
            self, bench):
        """Its value is the one thing a measurement decides.

        Left out of the declared preview rather than previewed wrongly, and
        added to the StepResult's own commands -- which is what the record and
        the audit trail read.
        """
        ctx = context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        step = next(s for s in result.steps if s.step_id == "standard-10kohm")
        assert step.commands[-1] == f"DELAY {OHMS_DELAY_SHORT_S:.2E}"
        assert len(step.commands) == 7
        for command in step.commands:
            assert command in bench.commands

    def test_the_meter_is_left_reconfigured_after_a_commit_too(self, bench):
        """Equally true whether or not calibration memory was written."""
        ctx = context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        assert result.committed is True
        assert any("function, range, OCOMP and DELAY" in w
                   for w in result.warnings)
        assert not any("dry run" in w for w in result.warnings)

    def test_a_significant_settling_difference_selects_the_long_delay(
            self, bench):
        bench.ohms_readings = [CERT_10KOHM * (1 - 100e-6), CERT_10KOHM]
        ctx = context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        assert any("will use DELAY 1 s" in w for w in result.warnings)
        assert any("project's" in w for w in result.warnings)
        # The last DELAY before the resistance CAL is the long one.
        assert _last_delay_before(bench.commands, f"{CAL_OHMS},{SECRET}") == \
            f"DELAY {OHMS_DELAY_LONG_S:.2E}"


# ── the wiring checks ─────────────────────────────────────────────────

class TestAcalDurationsMatchTheManual:
    """Calibration Manual p.87, "approximate time required" for each routine.

    Every one of these was short before this task -- by 15 s to 2 min -- and
    ``acal()`` sleeps the figure and returns, so three of the user's existing
    measurement tools (``tools/dmm_noise.py``, ``apps/dmm_bias.py``,
    ``apps/kvd_linearity.py``) began measuring a meter that was still
    autocalibrating.
    """

    @pytest.mark.parametrize("acal_type,seconds,quoted", [
        ("ALL", 16 * 60, "16 minutes"),
        ("DCV", 2 * 60 + 45, "2 minutes and 45 seconds"),
        ("AC", 2 * 60 + 45, "2 minutes and 45 seconds"),
        ("OHMS", 11 * 60, "11 minutes"),
    ])
    def test_p87(self, acal_type, seconds, quoted):
        from caldmm.drivers.hp3458a import ACAL
        assert int(ACAL[acal_type].duration) == seconds, quoted

    def test_none_is_shorter_than_it_was_documented_to_be(self):
        from caldmm.drivers.hp3458a import ACAL
        was = {"ALL": 840, "DCV": 150, "AC": 140, "OHMS": 600}
        for name, old in was.items():
            assert int(ACAL[name].duration) > old, name


class TestGrossErrorChecks:
    """This project's wiring gates, run in the NON-committing setup step."""

    def test_a_missing_short_stops_the_run_before_cal_zero(self, bench):
        bench.short_v = 0.05                          # 50 mV: no short
        ctx = context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        failed = next(s for s in result.steps
                      if s.outcome is StepOutcome.FAILED)
        assert failed.step_id == "short-front"
        assert "four-terminal short" in failed.error
        assert "project's wiring check" in failed.error
        assert bench.cal_commands == []

    def test_the_threshold_is_stated_and_modest(self):
        assert SHORT_MAX_V == pytest.approx(1e-3)

    def test_a_standard_that_is_not_connected_stops_the_run(self, bench):
        bench.standard_v = 0.0001                     # nothing on the leads
        ctx = context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        failed = next(s for s in result.steps
                      if s.outcome is StepOutcome.FAILED)
        assert failed.step_id == "standard-10v"
        assert "wiring or a typing mistake" in failed.error
        # The offsets went in; the dc gain adjustment did not.
        assert bench.cal_commands == [f"CAL 0,{SECRET}"] * 2

    def test_the_setup_commands_are_recorded_as_SENT_because_they_were(
            self, bench):
        """The check cannot run before I/O, so the record must not claim it did.

        Proving the short is fitted means selecting a function and a range and
        taking a reading first.  Filing those as ``skipped_commands`` -- which
        a ``SafetyError`` would -- would have the record assert the meter's
        function and range were left alone when they had just been changed.
        """
        bench.short_v = 0.05
        ctx = context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        failed = next(s for s in result.steps
                      if s.outcome is StepOutcome.FAILED)
        assert failed.commands == ("DCV 1.00E-01", "TARM SGL,1")
        assert failed.skipped_commands == ()
        # ...and they really did go out.
        for command in failed.commands:
            assert command in bench.commands
        # writes_nvram is False, so the NVRAM answer is still known-untouched.
        assert failed.nvram is NvramState.UNTOUCHED
        assert result.nvram_uncertain is False

    def test_a_wiring_failure_is_not_a_listed_guard(self):
        # SafetyError is documented as "before any I/O, by construction".
        assert not issubclass(WiringError, UNSENT_GUARDS)
        assert issubclass(WiringError, ProcedureError)

    def test_it_stops_the_run_before_the_operator_is_asked_to_commit(
            self, bench):
        """Raising, not returning a FAILED StepResult.

        ``run_procedure`` does not inspect a returned step's outcome, so
        returning would walk on to the very ``CAL`` this check exists to
        prevent -- and ask the operator to type the commit confirmation for it
        first.
        """
        console = FakeConsole()
        bench.short_v = 0.05
        ctx = context(bench, commit=True, console=console)
        result = run_procedure(PROC, ctx)
        assert console.confirm_explicit_calls == [], (
            "no irreversible write may be offered after a failed wiring check")
        assert bench.cal_commands == []
        assert "offset-front" in result.unreached

    def test_one_percent_is_the_gate(self):
        assert GAIN_CHECK_FRACTION == pytest.approx(0.01)


# ── the lockout ───────────────────────────────────────────────────────

class TestLockoutIsSurfacedNotWrapped:
    """The whole user experience of a lockout is that message."""

    def test_j132_and_the_top_cover_reach_the_record(self, bench):
        bench.auxerr_on_cal = AUXERR_CAL_RAM_WRITE_PROTECTION
        bench.err_on_cal = ERR_HARDWARE
        ctx = context(bench, commit=True)
        result = run_procedure(PROC, ctx)

        failed = next(s for s in result.steps
                      if s.outcome is StepOutcome.FAILED)
        assert failed.step_id == "offset-front"
        assert "CalibrationLockoutError" in failed.error
        assert "J132" in failed.error
        assert "top cover" in failed.error
        assert "03458-66505" in failed.error
        assert "no rear-panel calibration switch" in failed.error
        assert "SECURE" in failed.error
        # And it survives into the rendered summary the operator reads.
        assert "J132" in result.summary()

    def test_the_procedure_adds_no_vaguer_wrapper(self, bench):
        """The driver's exception reaches the caller as itself."""
        bench.auxerr_on_cal = AUXERR_CAL_RAM_WRITE_PROTECTION
        bench.err_on_cal = ERR_HARDWARE
        ctx = context(bench, commit=True)
        ctx.record(StepResult(step_id="short-front",
                              outcome=StepOutcome.EXECUTED,
                              started_at=_stamp(), finished_at=_stamp()))
        with pytest.raises(CalibrationLockoutError) as caught:
            PROC.adjust_step(ctx, "offset-front", 0.0, "FRONT offset")
        assert type(caught.value) is CalibrationLockoutError

    def test_a_lockout_stops_the_run_and_leaves_the_rest_unreached(self,
                                                                   bench):
        bench.auxerr_on_cal = AUXERR_CAL_RAM_WRITE_PROTECTION
        bench.err_on_cal = ERR_HARDWARE
        ctx = context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        assert "offset-rear" in result.unreached
        assert result.committed is False

    def test_a_plain_calibration_failure_also_stops_the_run(self, bench):
        bench.err_on_cal = ERR_CALIBRATION
        ctx = context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        failed = next(s for s in result.steps
                      if s.outcome is StepOutcome.FAILED)
        assert "CalibrationError" in failed.error
        assert result.committed is False

    def test_an_acal_failure_names_the_security_code_and_the_jumper(self,
                                                                    bench):
        bench.err_on_acal = ERR_CALIBRATION
        ctx = context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        failed = next(s for s in result.steps
                      if s.outcome is StepOutcome.FAILED)
        assert failed.step_id == "acal-ac"
        assert "J132" in failed.error
        assert "secured from the factory" in failed.error


# ── the security code ─────────────────────────────────────────────────

class TestSecurityCodeCannotInjectASecondCommand:
    """The 3458A takes several commands on one line.

    The driver validates the codes *it* assembles, but ``ACAL AC,<code>`` is
    assembled in this module and written with ``cmd()``, which transmits
    verbatim -- so that one command is the only calibration write the driver's
    guard never sees, and this module has to close it.
    """

    INJECTIONS = ["3458;RESET", "3458\nRESET", "3458,10", "3458 RESET",
                  "3458;CAL 0", "", "  "]

    @pytest.mark.parametrize("code", INJECTIONS[:5])
    def test_the_prompt_refuses_and_re_asks(self, bench, code):
        console = FakeConsole(answers=[
            "22", "1 h",
            code, SECRET,                  # rejected, then accepted
            f"{CERT_10V}", "1.5 ppm", "",
            f"{CERT_10KOHM}", "4 ppm", ""])
        ctx = ProcedureContext(device=meter(bench), console=console)
        PROC.prepare(ctx)
        assert ctx.security_code == SECRET
        assert "rejected" in console.text
        # The offending value is never echoed back at the operator.
        assert code not in console.text

    @pytest.mark.parametrize("code", ["", "   "])
    def test_a_blank_code_means_security_is_disabled(self, code):
        assert parse_security_code(code) is None

    @pytest.mark.parametrize("code", ["3458", "3458A", "-1", "0", "99887"])
    def test_documented_shapes_are_accepted(self, code):
        assert parse_security_code(code) == code

    def test_the_regex_mirrors_the_drivers(self):
        from caldmm.drivers import hp3458a as drv
        assert SECURITY_CODE_RE.pattern == drv._SECURITY_CODE_RE.pattern

    def test_a_hand_built_context_is_refused_before_the_wire(self, bench):
        ctx = context(bench, commit=True, security_code="3458;RESET")
        bench.written.clear()
        with pytest.raises(SafetyError, match="not alphanumeric"):
            require_valid_security_code(ctx, "CAL 0")
        assert bench.commands == []

    def test_preflight_blocks_it_before_the_certificates_are_typed(self,
                                                                   bench):
        ctx = context(bench, commit=True, security_code="3458;RESET")
        problems = PROC.preflight(ctx)
        assert any("not alphanumeric" in p for p in problems)

    def test_nothing_injected_reaches_the_wire_on_a_full_run(self, bench):
        """Preflight refuses it, so the run never starts at all."""
        ctx = context(bench, commit=True, security_code="3458;RESET")
        bench.written.clear()
        with pytest.raises(PreflightError, match="not alphanumeric"):
            run_procedure(PROC, ctx)
        assert "RESET" not in "\n".join(bench.commands)
        assert bench.cal_commands == []

    def test_the_acal_line_is_guarded_too_not_only_CAL(self, bench):
        """The regression this class exists for.

        ``adjust()`` validates its own argument; ``cmd('ACAL AC,<code>')``
        does not, so the guard has to be in the step.
        """
        ctx = context(bench, commit=True)
        step = next(s for s in PROC.steps(ctx) if s.id == "acal-ac")
        # Everything up to ACAL completed, so only the code can refuse it.
        for step_id in ("short-front", "offset-front", "short-rear",
                        "offset-rear", "standard-10v", "dc-gain",
                        "standard-10kohm", "resistance"):
            ctx.record(StepResult(step_id=step_id,
                                  outcome=StepOutcome.EXECUTED,
                                  started_at=_stamp(), finished_at=_stamp()))
        ctx.security_code = "3458;RESET"
        bench.written.clear()
        with pytest.raises(SafetyError, match="not alphanumeric"):
            step.action(ctx, step)
        assert bench.commands == [], "nothing may go out"

    def test_a_bad_code_is_never_recorded_as_cal_sent(self, bench):
        """The failure mode this closes, spelled out.

        A ``ValueError`` from the driver would be filed as "CAL sent, NVRAM
        unknown" -- and the operator told to treat the meter as possibly
        adjusted -- over a typo in a prompt.
        """
        ctx = context(bench, commit=True, security_code="3458;RESET")
        step = next(s for s in PROC.steps(ctx) if s.id == "offset-front")
        ctx.record(StepResult(step_id="short-front",
                              outcome=StepOutcome.EXECUTED,
                              started_at=_stamp(), finished_at=_stamp()))
        try:
            step.action(ctx, step)
        except Exception as exc:
            nvram, sent, skipped = classify_failure(
                exc, step.commands, writes_nvram=True)
        assert nvram is NvramState.UNTOUCHED
        assert sent == () and skipped == ("CAL 0",)


class TestSecurityCodeStaysOffTheRecord:
    def test_it_goes_on_the_wire(self, bench):
        ctx = context(bench, commit=True, security_code="99887")
        run_procedure(PROC, ctx)
        assert all(c.endswith(",99887") for c in bench.cal_commands)

    def test_it_appears_nowhere_in_the_result(self, bench):
        secret = "99887"
        ctx = context(bench, commit=True, security_code=secret)
        result = run_procedure(PROC, ctx)
        rendered = "\n".join([
            result.summary(), repr(result), repr(result.detail),
            "\n".join(result.detail["log"]), "\n".join(result.warnings),
            "\n".join(step.describe() for step in result.steps),
        ])
        assert secret not in rendered
        assert "CAL 0" in rendered            # the declared command is there

    def test_no_code_means_a_bare_cal(self, bench):
        ctx = context(bench, commit=True, security_code=None)
        run_procedure(PROC, ctx)
        assert bench.cal_commands[0] == "CAL 0"
        assert "," not in bench.cal_commands[0]


# ── prompting ─────────────────────────────────────────────────────────

class TestPrepareAsksForWhatItNeeds:
    def test_the_full_prompt_sequence(self, bench):
        console = FakeConsole(answers=[
            "22.5",                                   # ambient
            "3 h",                                    # since ACAL
            SECRET,                                   # security code
            f"{CERT_10V}", "1.5 ppm", "732B-001",     # 10 V standard
            f"{CERT_10KOHM}", "4 ppm", "742A-002",    # 10 kohm standard
        ])
        dev = meter(bench)
        ctx = ProcedureContext(device=dev, console=console)
        PROC.prepare(ctx)
        assert ctx.ambient_c == pytest.approx(22.5)
        assert ctx.acal_age_h == pytest.approx(3.0)
        assert ctx.security_code == SECRET
        assert set(ctx.standards) == {"10V", "10KOHM"}
        assert ctx.standards["10V"].certified == pytest.approx(CERT_10V)
        assert ctx.standards["10KOHM"].uncertainty_ppm == pytest.approx(4.0)
        assert console.answers == []

    def test_a_kohm_certificate_is_accepted_for_the_ohms_standard(self,
                                                                  bench):
        console = FakeConsole(answers=[
            "22", "1 h", "", "10.0000123", "1.5 ppm", "",
            "10.000003 kohm", "4 ppm", ""])
        ctx = ProcedureContext(device=meter(bench), console=console)
        PROC.prepare(ctx)
        assert ctx.standards["10KOHM"].certified == pytest.approx(10000.003)
        assert ctx.security_code is None

    def test_a_prefilled_ambient_is_not_asked_for(self, bench):
        console = FakeConsole(answers=[
            "1 h", "", f"{CERT_10V}", "1.5 ppm", "",
            f"{CERT_10KOHM}", "4 ppm", ""])
        ctx = ProcedureContext(device=meter(bench), console=console,
                               ambient_c=21.0)
        PROC.prepare(ctx)
        assert ctx.ambient_c == 21.0
        assert not any("ambient" in q for q in console.asked)

    def test_a_rejected_ambient_is_re_asked(self, bench):
        console = FakeConsole(answers=[
            "40", "22", "1 h", "", f"{CERT_10V}", "1.5 ppm", "",
            f"{CERT_10KOHM}", "4 ppm", ""])
        ctx = ProcedureContext(device=meter(bench), console=console)
        PROC.prepare(ctx)
        assert ctx.ambient_c == 22.0
        assert "rejected" in console.text


class TestTheBriefSaysWhatMatters:
    def test_it_is_required_and_declining_it_stops_the_run(self, bench):
        console = FakeConsole(ok=lambda what: "brief" not in what)
        ctx = context(bench, commit=True, console=console)
        result = run_procedure(PROC, ctx)
        assert result.error.startswith("brief:")
        assert bench.cal_commands == []

    def test_it_distinguishes_cal_from_acal_and_names_the_warm_up(self,
                                                                  bench):
        ctx = context(bench)
        step = next(s for s in PROC.steps(ctx) if s.id == "brief")
        body = "\n".join(step.body)
        assert "It is NOT ACAL" in body
        assert "4 HOURS" in body
        assert "no ONTIME? on a 3458A" in body
        assert "OVERWRITES" in body

    def test_it_states_what_the_procedure_does_not_do(self, bench):
        ctx = context(bench)
        step = next(s for s in PROC.steps(ctx) if s.id == "brief")
        body = "\n".join(step.body)
        assert "AC Adjustment" in body and "SCAL" in body
        assert "ACAL OHMS" in body
        assert len(NOT_IMPLEMENTED_NOTES) == 2


# ── the declared commands are not a lie ───────────────────────────────

class TestDeclaredCommandsMatchTheWire:
    """``AdjustStep.commands`` is what a dry run shows the operator."""

    def test_every_nvram_step_declares_exactly_what_it_sends(self, bench):
        ctx = context(bench, commit=True)
        declared = [step.commands for step in PROC.steps(ctx)
                    if step.writes_nvram]
        assert declared == [(CAL_ZERO,), (CAL_ZERO,), (CAL_GAIN,),
                            (CAL_OHMS,), ("ACAL AC",)]
        run_procedure(PROC, ctx)
        # On the wire, with the security code appended and nothing else.
        assert [c.split(",")[0] for c in bench.cal_commands] == [
            CAL_ZERO, CAL_ZERO, CAL_GAIN, CAL_OHMS, "ACAL AC"]

    def test_the_setup_steps_declared_commands_all_appear_in_order(self,
                                                                   bench):
        ctx = context(bench, commit=False)
        steps = {step.id: step for step in PROC.steps(ctx)}
        run_procedure(PROC, ctx)
        commands = bench.commands
        for step_id in ("short-front", "standard-10v", "standard-10kohm"):
            position = -1
            for declared in steps[step_id].commands:
                assert declared in commands, (step_id, declared)
                position = commands.index(declared, position + 1)

    def test_the_conditional_ohms_delay_is_documented_not_declared(self,
                                                                   bench):
        ctx = context(bench)
        step = next(s for s in PROC.steps(ctx) if s.id == "standard-10kohm")
        assert step.commands[-1] == "TARM SGL,1"
        assert any("longer DELAY" in line for line in step.body)


# ── the sourced mode ──────────────────────────────────────────────────
#
# ``--source fluke5700a:...``: a calibrator stands in for the two gain
# artefacts.  The meter side is unchanged -- the real HP3458A driver over the
# same FakeGpib -- and the calibrator is a fake ``SourceRole`` whose readback
# deliberately differs from the nominal, because the whole point of the mode
# is that the readback, not the nominal, reaches ``CAL``.

#: Readbacks that are visibly not the nominals.  The ohms one models the
#: 5700A's characterized resistance, a few hundred ppm from what was asked.
SRC_10V = 10.0000200
SRC_10KOHM = 10000.0 * (1 + 320e-6)          # 10003.2

CAL_SRC_GAIN = f"CAL {SRC_10V:.10G}"
CAL_SRC_OHMS = f"CAL {SRC_10KOHM:.10G}"


class FakeSource:
    """A ``SourceRole`` double with an event log.

    The same fake-source pattern as the 34401A suite's calibrator: apply
    records the point and answers a readback that is not the nominal, standby
    is counted, and health is scripted -- so a test can prove the standby
    discipline and the readback plumbing without a driver.
    """

    model_id = "fluke5700a"

    def __init__(self, *, health=None, refuse=None,
                 dcv_actual=SRC_10V, ohms_actual=SRC_10KOHM, gpib=None,
                 probe_scale=1.0):
        self.health_answer = health if health is not None else SourceHealth(
            warm_minutes=120, days_since_zero=5, days_since_cal=60)
        self.refuse = dict(refuse or {})
        self.dcv_actual = dcv_actual
        self.ohms_actual = ohms_actual
        #: The meter, so a §8 probe puts its level where the meter will read
        #: it.  A real bench does this with a wire.
        self.gpib = gpib
        #: What the meter sees relative to what is sourced, at a probe.  1.0
        #: is a correctly wired bench; 0.0 is an open lead.
        self.probe_scale = float(probe_scale)
        self.applied = []
        self.standby_calls = 0
        self.events = []                     # "OPER <label>" / "STBY"

    def validate(self, point):
        return list(self.refuse.get(point.quantity, ()))

    def apply(self, point):
        self.applied.append(point)
        self.events.append(f"OPER {point.label}")
        probe = str(getattr(point, "label", "")).startswith("wiring proof")
        if point.quantity is Quantity.RES:
            actual = (self.ohms_actual if not probe
                      else float(point.value) * self.ohms_actual / 10000.0)
            state = SourceState(
                actual_value=actual, unit="OHM", range_id="R10K",
                uncertainty=Uncertainty(8.0, "PPM", 0.95),
                settled=True, operate=True)
        else:
            actual = (self.dcv_actual if not probe
                      else float(point.value) * self.dcv_actual / 10.0)
            state = SourceState(
                actual_value=actual, unit="V", range_id="DC22V",
                uncertainty=Uncertainty(3.5, "PPM", 0.95),
                settled=True, operate=True)
        if probe and self.gpib is not None:
            self.gpib.probe_reading = actual * self.probe_scale
        return state

    def standby(self):
        self.standby_calls += 1
        self.events.append("STBY")

    def health(self):
        return self.health_answer


def sourced_context(bench, *, commit=False, source=None, **kwargs):
    """A prepared sourced-mode context: a calibrator and **no** standards."""
    ctx = context(bench, commit=commit, **kwargs)
    ctx.standards = {}          # nothing typed in; the readback replaces both
    ctx.source = (source if source is not None
                  else FakeSource(gpib=bench))
    return ctx


class TestSourcedModeDeclaration:
    """The three-way policy: accepted, never required."""

    def test_the_policy_is_optional(self):
        assert HP3458AExternalAdjust.accepts_source is True
        assert needs_source(PROC) is False
        assert source_policy(PROC) == "optional"

    def test_the_registered_instance_answers_the_same(self):
        assert source_policy(get_procedure("hp3458a", PROCEDURE_ID)) == \
            "optional"

    def test_the_two_sourced_points_are_the_chapter_3_values(self):
        assert SOURCED_POINT_10V.quantity is Quantity.DCV
        assert SOURCED_POINT_10V.value == 10.0
        assert SOURCED_POINT_10KOHM.quantity is Quantity.RES
        assert SOURCED_POINT_10KOHM.value == 10000.0

    def test_the_ohms_point_is_declared_four_wire(self):
        """FOUR_WIRE is what makes the adapter assert EXTSENSE: the meter is
        on OHMF and a two-wire sourcing would put the lead resistance into an
        irreversible CAL."""
        assert SOURCED_POINT_10KOHM.connection is Connection.FOUR_WIRE
        assert SOURCED_POINT_10V.connection is Connection.TWO_WIRE


class TestSourcedPrepareAndPreflight:
    def test_prepare_skips_the_certificate_prompts(self, bench):
        """Ambient, ACAL age and the security code are still asked; the two
        certificates are not -- the readback replaces both."""
        console = FakeConsole(answers=["22.5", "3 h", SECRET])
        ctx = ProcedureContext(device=meter(bench), console=console)
        ctx.source = FakeSource()
        PROC.prepare(ctx)
        assert ctx.ambient_c == pytest.approx(22.5)
        assert ctx.security_code == SECRET
        assert ctx.standards == {}
        assert console.answers == [], "exactly three questions are asked"
        assert not any("certified" in q for q in console.asked)

    def test_a_sourced_context_needs_no_standards(self, bench):
        assert PROC.preflight(sourced_context(bench)) == []

    def test_without_a_source_missing_standards_still_block(self, bench):
        """The artefact mode's demand is untouched."""
        ctx = context(bench)
        ctx.standards = {}
        assert any("no certified value" in p for p in PROC.preflight(ctx))

    def test_the_envelope_is_checked_offline(self, bench):
        """Before anything is opened or energized -- finding out at the
        resistance step would leave the dc gain adjusted and the ohms not."""
        source = FakeSource(refuse={
            Quantity.RES: ("10 kΩ four-wire is outside this calibrator's "
                           "envelope",)})
        problems = PROC.preflight(sourced_context(bench, source=source))
        assert any("refuses it" in p and "10 kΩ" in p for p in problems)

    def test_sourced_envelope_problems_names_both_points(self, bench):
        source = FakeSource(refuse={Quantity.DCV: ("no dc",),
                                    Quantity.RES: ("no ohms",)})
        problems = sourced_envelope_problems(source)
        assert len(problems) == 2
        assert any("10 V" in p for p in problems)
        assert any("10 kΩ" in p for p in problems)

    def test_calibrator_health_gates_the_run(self, bench):
        """The same checks, thresholds and blocking force as the 34401A's
        sourced adjustment."""
        source = FakeSource(health=SourceHealth(
            faults=((541, "External sense fault"),),
            warm_minutes=10, days_since_zero=45))
        problems = PROC.preflight(sourced_context(bench, source=source))
        assert any("queued faults" in p and "541" in p for p in problems)
        assert any("warm for 10 min" in p for p in problems)
        assert any("dc zeros are 45 days old" in p for p in problems)

    def test_a_healthy_calibrator_raises_no_problems(self, bench):
        assert PROC.preflight(sourced_context(bench)) == []

    def test_an_unreadable_health_blocks_rather_than_crashes(self, bench):
        source = FakeSource()
        source.health = lambda: (_ for _ in ()).throw(OSError("no listener"))
        problems = PROC.preflight(sourced_context(bench, source=source))
        assert any("would not report its health" in p for p in problems)

    def test_an_object_that_is_not_a_source_is_refused_by_precheck(
            self, bench):
        ctx = sourced_context(bench, source=object())
        assert any("SourceRole" in p for p in PROC.precheck(ctx))
        assert any("SourceRole" in p for p in PROC.preflight(ctx))


class TestSourcedSafeSequencing:
    """Global Constraints §8 and §9 in the 3458A's sourced steps."""

    def test_the_safe_level_precedes_the_first_sourced_volt(self, bench):
        ctx = sourced_context(bench, commit=True)
        run_procedure(PROC, ctx)
        volts = [float(p.value) for p in ctx.source.applied
                 if p.quantity is Quantity.DCV]
        # 0.1 % of the 10 V point's full scale is below the 10 mV floor.
        assert volts[0] == pytest.approx(0.01)
        assert volts[1] == pytest.approx(10.0)

    def test_an_open_lead_stops_before_the_standard_is_applied(self, bench):
        ctx = sourced_context(bench, commit=True,
                              source=FakeSource(gpib=bench, probe_scale=0.0))
        result = run_procedure(PROC, ctx)
        assert not result.complete
        volts = [float(p.value) for p in ctx.source.applied
                 if p.quantity is Quantity.DCV]
        assert volts == [pytest.approx(0.01)]
        assert not any(c.startswith("CAL 1") for c in bench.cal_commands)

    def test_the_meter_is_configured_before_the_source_operates(self, bench):
        """H03/§9, in the step that first energizes the calibrator."""
        ctx = sourced_context(bench, commit=True)
        events = []
        real_apply = ctx.source.apply

        def recording(point):
            events.append("source")
            return real_apply(point)

        ctx.source.apply = recording
        real_write = bench.write

        def recording_write(cmd):
            if cmd.strip().startswith(("DCV ", "OHMF ")):
                events.append("meter")
            return real_write(cmd)

        bench.write = recording_write
        step = PROC._sourced_dc_standard_step()
        step.action(ctx, step)
        assert events[0] == "meter"

    def test_the_proof_is_labelled_in_the_record(self, bench):
        ctx = sourced_context(bench, commit=True)
        run_procedure(PROC, ctx)
        labels = [str(getattr(p, "label", "")) for p in ctx.source.applied]
        assert any(label.startswith("wiring proof") for label in labels)


class TestSourcedRun:
    def test_the_dc_gain_cal_uses_the_readback_not_the_nominal(self, bench):
        ctx = sourced_context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        assert result.committed is True, result.summary()
        assert f"{CAL_SRC_GAIN},{SECRET}" in bench.cal_commands
        assert f"CAL 10,{SECRET}" not in bench.cal_commands
        assert SRC_10V != 10.0               # or this test proves nothing

    def test_the_resistance_cal_uses_the_characterized_readback(self, bench):
        """The 5700A's ohms sit up to 500 ppm from nominal by design; the
        nominal would fold that into the meter's gain constants."""
        ctx = sourced_context(bench, commit=True)
        run_procedure(PROC, ctx)
        assert f"{CAL_SRC_OHMS},{SECRET}" in bench.cal_commands
        assert f"CAL 10000,{SECRET}" not in bench.cal_commands

    def test_the_full_sourced_sequence_in_the_documented_order(self, bench):
        ctx = sourced_context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        assert bench.cal_commands == [
            f"CAL 0,{SECRET}",               # front offset: the hand short
            f"CAL 0,{SECRET}",               # rear offset: the hand short
            f"{CAL_SRC_GAIN},{SECRET}",      # dc gain: the 10 V readback
            f"{CAL_SRC_OHMS},{SECRET}",      # resistance: the ohms readback
            f"ACAL AC,{SECRET}",
        ]
        assert result.complete is True

    def test_the_step_ids_match_the_artefact_mode(self, bench):
        """Same ids, so ADJUSTMENT_ORDER holds identically in both modes."""
        artefact_ids = [s.id for s in PROC.steps(context(bench))]
        sourced_ids = [s.id for s in PROC.steps(sourced_context(bench))]
        assert sourced_ids == artefact_ids

    def test_the_short_and_offset_steps_are_identical_in_both_modes(
            self, bench):
        """A low-thermal 4-wire short by hand, in both modes: the 5700A's
        0 Ω through leads and output relays is not a low-thermal short."""
        artefact = {s.id: s for s in PROC.steps(context(bench))}
        sourced = {s.id: s for s in PROC.steps(sourced_context(bench))}
        for step_id in ("short-front", "offset-front", "short-rear",
                        "offset-rear"):
            assert sourced[step_id].prompt == artefact[step_id].prompt
            assert sourced[step_id].commands == artefact[step_id].commands
            assert sourced[step_id].body == artefact[step_id].body

    def test_the_source_is_never_asked_to_source_the_short(self, bench):
        ctx = sourced_context(bench, commit=True)
        source = ctx.source
        run_procedure(PROC, ctx)
        assert all(p.value != 0.0 for p in source.applied)
        assert {p.quantity for p in source.applied} == {Quantity.DCV,
                                                        Quantity.RES}

    def test_the_ohms_sourcing_is_four_wire_every_time(self, bench):
        ctx = sourced_context(bench, commit=True)
        source = ctx.source
        run_procedure(PROC, ctx)
        ohm_points = [p for p in source.applied
                      if p.quantity is Quantity.RES]
        assert len(ohm_points) == 2          # the setup step and the CAL step
        assert all(p.connection is Connection.FOUR_WIRE for p in ohm_points)

    def test_standby_after_every_energization_on_success(self, bench):
        """The exact OPER/STBY sequence of a whole sourced run.

        Six OPERs, not four: the two dc-volts steps prove their wiring at
        10 mV first (§8).  Six STBYs, not four: a probe is preceded by one of
        its own, because ``apply`` is the only thing that de-energizes and a
        probe would otherwise re-range the meter down while the previous
        point is still live.  The pattern per proved step is therefore
        STBY, OPER(proof), OPER(point), STBY; per unproved step OPER, STBY.
        """
        ctx = sourced_context(bench, commit=True)
        source = ctx.source
        run_procedure(PROC, ctx)
        assert source.events == [
            # standard-10v: de-energize, prove at 10 mV, then the standard
            "STBY", "OPER wiring proof before the sourced 10 V standard",
            "OPER sourced 10 V dc gain point", "STBY",
            # dc-gain: the same, and the CAL is sent from inside it
            "STBY", "OPER wiring proof before the sourced 10 V standard",
            "OPER sourced 10 V dc gain point", "STBY",
            # standard-10kohm and resistance: the ohms proof IS the step's
            # own first reading of the standard, checked tighter than 10 %
            "OPER sourced 10 kΩ resistance point", "STBY",
            "OPER sourced 10 kΩ resistance point", "STBY",
        ]
        assert source.standby_calls == 6

    def test_a_dry_run_still_sources_and_measures_but_commits_nothing(
            self, bench):
        ctx = sourced_context(bench, commit=False)
        source = ctx.source
        result = run_procedure(PROC, ctx)
        assert bench.cal_commands == []
        # The two setup steps really sourced their points and went back to
        # standby; the two committing steps were held back entirely.  Three
        # applications, not two: the dc-volts one proves its wiring at 10 mV
        # first (§8), which is exactly the rehearsal a dry run is for.
        assert len(source.applied) == 3
        assert source.standby_calls == 3    # one of them precedes the probe
        assert source.events[-1] == "STBY"
        assert any("readback replaces this value" in c
                   for c in result.would_commit)

    def test_the_uncertainty_reaches_the_audit_trail(self, bench):
        """SourceState.uncertainty, recorded per step: the record has no
        certificate to cite, so the calibrator's own figure is what the
        meter's accuracy now inherits."""
        ctx = sourced_context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        steps = {s.step_id: s for s in result.steps}

        setup_v = steps["standard-10v"].detail
        assert setup_v["source_actual_v"] == pytest.approx(SRC_10V)
        assert setup_v["source_uncertainty"].value == pytest.approx(3.5)
        assert setup_v["source_uncertainty"].unit == "PPM"

        gain = steps["dc-gain"].detail
        assert gain["source_actual"] == pytest.approx(SRC_10V)
        assert gain["source_uncertainty"].value == pytest.approx(3.5)
        assert gain["value"] == pytest.approx(SRC_10V)   # what CAL received

        setup_o = steps["standard-10kohm"].detail
        assert setup_o["source_uncertainty"].value == pytest.approx(8.0)

        ohms = steps["resistance"].detail
        assert ohms["source_actual"] == pytest.approx(SRC_10KOHM)
        assert ohms["source_uncertainty"].value == pytest.approx(8.0)

    def test_the_record_names_what_stood_in_for_the_certificates(self, bench):
        ctx = sourced_context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        assert result.detail["sourced_by"] == "fluke5700a"
        assert result.standards == ()
        artefact_result = run_procedure(PROC, context(bench, commit=False))
        assert artefact_result.detail["sourced_by"] is None

    def test_the_gross_error_gate_runs_against_the_readback(self, bench):
        """A readback 5 % from nominal with the meter agreeing passes; the
        meter reading the NOMINAL is what gets refused.  Both legs together
        prove the gate's reference is the readback."""
        bench.standard_v = 10.5
        ctx = sourced_context(bench, commit=True,
                              source=FakeSource(dcv_actual=10.5, gpib=bench))
        run_procedure(PROC, ctx)
        assert f"CAL {10.5:.10G},{SECRET}" in bench.cal_commands

    def test_a_meter_reading_the_nominal_against_a_shifted_readback_refuses(
            self, bench):
        bench.standard_v = 10.0
        ctx = sourced_context(bench, commit=True,
                              source=FakeSource(dcv_actual=10.5, gpib=bench))
        result = run_procedure(PROC, ctx)
        failed = next(s for s in result.steps
                      if s.outcome is StepOutcome.FAILED)
        assert failed.step_id == "standard-10v"
        assert "calibrator reports" in failed.error
        assert not any(c.startswith("CAL 1") for c in bench.cal_commands)

    def test_a_wiring_fault_stops_the_run_and_stands_the_source_down(
            self, bench):
        bench.standard_v = 0.0001            # nothing on the leads
        ctx = sourced_context(bench, commit=True)
        source = ctx.source
        result = run_procedure(PROC, ctx)
        failed = next(s for s in result.steps
                      if s.outcome is StepOutcome.FAILED)
        assert failed.step_id == "standard-10v"
        assert "NOTHING WAS SENT TO CALIBRATION MEMORY" in failed.error
        # The offsets went in by hand; no gain CAL was reached.
        assert bench.cal_commands == [f"CAL 0,{SECRET}"] * 2
        assert "dc-gain" in result.unreached
        # ...and the output is dark.
        assert source.events[-1] == "STBY"
        assert source.standby_calls == 2    # the probe's, and the step's

    def test_a_cal_failure_still_stands_the_source_down(self, bench):
        """The finally-standby holds on the committing step's failure path
        too: the output must not be live while a lockout is investigated."""
        original = bench._dispatch

        def failing(cmd):
            original(cmd)
            if cmd.split(",")[0] == CAL_SRC_GAIN:
                bench.err |= ERR_CALIBRATION
        bench._dispatch = failing

        ctx = sourced_context(bench, commit=True)
        source = ctx.source
        result = run_procedure(PROC, ctx)
        failed = next(s for s in result.steps
                      if s.outcome is StepOutcome.FAILED)
        assert failed.step_id == "dc-gain"
        assert "resistance" in result.unreached
        assert source.events[-1] == "STBY"
        # The setup step and this one, each preceded by its probe's own.
        assert source.standby_calls == 4

    def test_the_ohms_settling_comparison_is_kept(self, bench):
        """The p.44 note does not stop applying because the standard is a
        calibrator: a significant fast/slow spread still selects the long
        DELAY for the adjustment."""
        bench.ohms_readings = [SRC_10KOHM * (1 - 100e-6), SRC_10KOHM]
        ctx = sourced_context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        assert any("will use DELAY 1 s" in w for w in result.warnings)
        assert _last_delay_before(bench.commands,
                                  f"{CAL_SRC_OHMS},{SECRET}") == \
            f"DELAY {OHMS_DELAY_LONG_S:.2E}"

    def test_every_write_still_needs_its_own_typed_confirmation(self, bench):
        console = FakeConsole()
        ctx = sourced_context(bench, commit=True, console=console)
        run_procedure(PROC, ctx)
        assert len(console.confirm_explicit_calls) == 5
        # The sourced confirmations say what the reference really was.
        assert any("not a certified artefact" in what
                   for what in console.confirm_explicit_calls)

    def test_the_artefact_mode_is_completely_unchanged_without_a_source(
            self, bench):
        """No source on the context: the wire sees exactly the sequence the
        artefact tests above pin, certificates and all."""
        ctx = context(bench, commit=True)
        assert getattr(ctx, "source", None) is None
        run_procedure(PROC, ctx)
        assert bench.cal_commands == [
            f"CAL 0,{SECRET}", f"CAL 0,{SECRET}",
            f"{CAL_GAIN},{SECRET}", f"{CAL_OHMS},{SECRET}",
            f"ACAL AC,{SECRET}"]


# ── finalize ──────────────────────────────────────────────────────────

class TestFinalize:
    def test_it_records_the_standards_the_ambient_and_the_log(self, bench):
        ctx = context(bench, commit=True)
        result = run_procedure(PROC, ctx)
        assert result.model_id == "hp3458a"
        assert result.procedure_id == PROCEDURE_ID
        assert [s.key for s in result.standards] == ["10V", "10KOHM"]
        assert result.ambient_c == pytest.approx(22.5)
        assert result.operator == "tester"
        assert result.detail["acal_age_h"] == pytest.approx(2.0)
        assert result.detail["not_implemented"] == NOT_IMPLEMENTED_NOTES
        assert result.detail["log"]

    def test_it_works_after_an_interrupt(self, bench):
        ctx = context(bench, commit=True)
        ctx.interrupted = True
        result = PROC.finalize(ctx)
        assert result.interrupted is True
        assert result.steps == ()

    def test_the_summary_names_the_procedure_and_the_mode(self, bench):
        ctx = context(bench, commit=False)
        result = run_procedure(PROC, ctx)
        summary = result.summary()
        assert "DRY RUN" in summary
        assert "hp3458a" in summary
        assert "NOT committed" in summary


class TestProcedureIsStateless:
    """One registered instance is shared; nothing may accumulate on it."""

    def test_two_runs_against_the_same_instance_do_not_interfere(self, bench):
        first = context(bench, commit=False)
        run_procedure(PROC, first)
        second = context(bench, commit=False)
        result = run_procedure(PROC, second)
        assert result.complete is False        # dry run: writes held back
        assert len(result.steps) == len(first.results)

    def test_the_instance_has_no_run_state(self):
        assert vars(PROC) == {}


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

    import caldmm.cal.dmm_procedures as dp
    from caldmm.cal.procedure import (
        ProcedureContext, StepOrderError, get_procedure)

    # Usable, not merely importable.  The device is duck-typed, so a stub with
    # no driver behind it is a legal instrument here.
    class Stub:
        def adjust(self, value, security_code=None):
            raise AssertionError("nothing may be sent by this test")

    proc = get_procedure("hp3458a", dp.PROCEDURE_ID)
    ctx = ProcedureContext(device=Stub(), console=None, ambient_c=23.0)

    # The mandated ch.3 order is enforced before any I/O ...
    try:
        proc.require_order(ctx, "resistance")
    except StepOrderError as exc:
        assert "dc-gain" in str(exc), exc
    else:
        raise AssertionError("out-of-order resistance step was not refused")

    # ... and so are the CAL bands mirrored from the driver, which is why they
    # are mirrored: the guard has to fire in a process where hp3458a has not
    # been imported and may not be importable.
    assert dp.require_in_band(10.0, "CAL 10") == "dc_gain"
    assert dp.require_in_band(10000.0, "CAL 10000") == "resistance"

    leaked = sorted(m for m in sys.modules if m.startswith("caldmm.drivers"))
    assert not leaked, "cal.dmm_procedures pulled in drivers: " + repr(leaked)
    assert "caldmm.cal.adapters" not in sys.modules, \\
        "cal.dmm_procedures imported adapters"
    print("OK")
    """)


def test_dmm_procedures_imports_and_guards_with_no_hardware_libraries():
    """The one driver import in this module is **lazy**, and stays lazy.

    ``hp3458a`` is imported inside :meth:`HP3458AExternalAdjust._acal_ac_step`'s
    action, for the ``ACAL`` duration and the error-bit decoders -- see the
    module docstring's "Import hygiene" section and ``cal/__init__``.  That is a
    sanctioned second lazy-import site, but only while it is lazy: hoisting it
    to module scope would make ``cal`` unimportable on a machine without
    linux-gpib and take the whole verification runner with it, and nothing else
    in the suite noticed.

    A subprocess with a real ``sys.meta_path`` blocker, because the root
    conftest stubs ``Gpib``/``serial``/``pyvisa``/``smbus`` -- in-process, the
    hoisted import would simply succeed.
    """
    completed = subprocess.run([sys.executable, "-c", IMPORT_HYGIENE_SCRIPT],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert "OK" in completed.stdout

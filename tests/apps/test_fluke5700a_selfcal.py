"""Tests for caldmm.apps.fluke5700a_selfcal (task D4).

Offline only -- no hardware, no linux-gpib, nothing that blocks.

The instrument is a **real** :class:`caldmm.drivers.fluke5700a.Fluke5700A`
driving :class:`tests.drivers.fake_gpib.FakeTransport`, and the operator is
``builtins.input`` monkeypatched with :class:`Operator`, so the app runs through
its real console (:class:`caldmm.apps.fluke5700a_selfcal.TranscriptConsole`,
a real :class:`~caldmm.lib.console.OperatorConsole` subclass).  Nothing here is
a mock of the thing under test.

Two rules this file follows, both learned the hard way in this project:

* **Assert against the transport log, not against a message.**  "the app
  printed that it did not store" is not evidence that ``CAL_STORE`` was
  withheld; ``"CAL_STORE" not in transport.log`` is.
* **Build every fixture through the real constructor.**  A
  :class:`~caldmm.cal.procedure.ProcedureResult` here is only ever the one
  :func:`~caldmm.cal.procedure.run_procedure` produced, so no test can assert
  against a state the system cannot emit.
"""

import os
import tempfile
from unittest.mock import patch

import pytest

from caldmm.apps import fluke5700a_selfcal as sc
from caldmm.cal.procedure import (
    CalibrationSwitchLockout,
    NvramState,
    ProcedureContext,
    StepOutcome,
    classify_failure,
    get_procedure,
    run_procedure,
)
from caldmm.drivers.fluke5700a import Fluke5700A, ISR_REMOTE

from tests.drivers.fake_gpib import FakeTransport


GOOD_IDN = "FLUKE,5720A,5248000,1.0+B+*"

#: The three certificates the tests type in, in the order the procedure asks.
#: Real-looking numbers: a 732A a few ppm high, a 742A-10k a few ppm low.
CERTIFICATES = [
    ("10.00013", "1.5 ppm"),
    ("9.99987", "4 ppm"),
    ("1.0000260", "10 ppm"),
]

#: What a full artifact calibration puts on the wire, committing run.
ARTIFACT_COMMANDS = [
    "CAL_TEMP 23.5",
    "CAL_REF 1.0000130000E+01 V;*OPC",
    "CAL_REF -1.0000130000E+01 V;*OPC",
    "CAL_REF 9.9998700000E+00 KOHM;*OPC",
    "CAL_REF 1.0000260000E+00 OHM;*OPC",
    "CAL_ADJ;*OPC",
    "CAL_STORE;*WAI",
]


# ── the instrument ────────────────────────────────────────────────────

def _report_lines(first_pct, second_pct):
    """Two output-shift rows that ``caldmm.cal.calreport`` really parses."""
    return [
        "FLUKE CORPORATION   S/N 5248000   5700A OUTPUT CALIBRATION",
        "PRINTED ON  1/02/26 AT 03:04:05 5700A  S/N 5248000",
        "DC VOLTAGE OUTPUT SHIFTS",
        "RANGE Point     Zero Shift       Full Scale Shift          Spec   Shift",
        "(+/-) (% spec)",
        f"220 mV +FS     0.00003 mV    -0.00041 mV  -1.87   ppm   9.91 ppm  "
        f"{first_pct}",
        f"2.2 V +FS      0.00002 mV     0.00021 mV   0.95   ppm   9.10 ppm  "
        f"{second_pct}",
    ]


def _block(lines):
    return ['"', *lines, '"']


SLST_BLOCK = _block([
    "1", "DC2_2V,1",
    "2.00E+00,0.00E+00,1.10E-06,2.30E-06,1.15E+00,9.10E+00,1.26E+01",
])

BEFORE_REPORT = "\n".join(_report_lines("-18.88", "10.44"))
AFTER_REPORT = "\n".join(_report_lines("-2.10", "1.02"))


def default_responses(**overrides):
    """Everything a full session queries, with plausible answers.

    ``*ESR?`` answers ``1`` (the OPC bit), so every long-term command completes
    on its first poll: the driver's real ``_run_long_term`` loop runs, quickly.
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


def mutating(transport):
    """Only the commands that change the instrument, in order."""
    return [cmd for cmd in transport.log
            if cmd.startswith("CAL_") and "?" not in cmd]


class FaultScript:
    """``FAULT?`` answers ``0``, except once per ``CAL_STORE``.

    Models the rear-panel CALIBRATION switch left in NORMAL: the calibrator
    queues fault 845 and stores nothing.  *lockouts* is how many store attempts
    are refused before one is accepted, so a single instance drives both the
    failure and the single-step retry that recovers from it.
    """

    def __init__(self, transport, *, code=845, lockouts=1):
        self.transport = transport
        self.code = code
        self.lockouts = lockouts
        self.seen_stores = 0
        self.pending = False

    def __call__(self, _cmd):
        stores = sum(1 for cmd in self.transport.log
                     if cmd.startswith("CAL_STORE"))
        if stores > self.seen_stores:
            self.seen_stores = stores
            if self.lockouts > 0:
                self.lockouts -= 1
                self.pending = True
        if self.pending:
            self.pending = False
            return str(self.code)
        return "0"


def sabotage_log_after_store(transport, log_path):
    """``FAULT?`` answers ``0``, and the session log's path stops being usable.

    A directory appears where the log file goes, the moment ``CAL_STORE`` has
    been sent -- the shape "the disk filled / the share went away during the
    run" takes for the code under test. The commit succeeds; the record then
    cannot be written to the path the operator asked for.
    """
    def answer(_cmd):
        if (any(cmd.startswith("CAL_STORE") for cmd in transport.log)
                and not os.path.isdir(log_path)):
            os.mkdir(log_path)
        return "0"
    return answer


def dead_after_store(transport):
    """``FAULT?`` stops answering once ``CAL_STORE`` has gone out.

    The commit reached the wire and the drain behind it timed out: the EEPROM
    has probably been written and there is no way to find out from here.  That
    is the ``nvram_uncertain`` state, produced the only way the system can
    produce it -- by a real driver failing on a real transport.
    """
    def answer(_cmd):
        if any(cmd.startswith("CAL_STORE") for cmd in transport.log):
            return TimeoutError("fake GPIB read timeout")
        return "0"
    return answer


# ── the operator ──────────────────────────────────────────────────────

class Operator:
    """Answers ``input()`` by reading the prompt, the way a person does.

    A fixed queue of answers breaks whenever a prompt is added or removed and
    silently answers the wrong question when one moves, which for a program
    whose prompts gate an irreversible write is not an acceptable failure mode.
    So each answer is chosen by what is being asked.
    """

    def __init__(self, *, certificates=None, ambient="23.5", enter="",
                 ok="y", explicit="yes", retry="y", ok_by_step=None,
                 interrupt_on=None):
        self.certificates = list(certificates or CERTIFICATES)
        self.ambient = ambient
        self.enter = enter
        self.ok = ok
        self.explicit = explicit
        self.retry = retry
        self.ok_by_step = dict(ok_by_step or {})
        self.interrupt_on = interrupt_on
        self.prompts: "list[str]" = []
        self._index = 0

    def __call__(self, prompt=""):
        self.prompts.append(prompt)
        if self.interrupt_on and self.interrupt_on in prompt:
            raise KeyboardInterrupt
        if "certified value" in prompt:
            return self._certificate()[0]
        if "uncertainty" in prompt:
            answer = self._certificate()[1]
            self._index += 1                  # this standard is done
            return answer
        if "serial / certificate" in prompt:
            return "SN-TEST"
        if "ambient temperature" in prompt:
            return self.ambient
        if "press Enter" in prompt:
            return self.enter
        if "retrying CAL_STORE" in prompt:
            return self.retry
        if "OK with" in prompt:
            for step_id, answer in self.ok_by_step.items():
                if f"step {step_id}?" in prompt:
                    return answer
            return self.ok
        if "Override and accept" in prompt:
            return self.explicit
        raise AssertionError(f"unscripted prompt: {prompt!r}")

    def _certificate(self):
        # The last entry repeats, which is what drives the "typed the same
        # decade error five times" path.
        index = min(self._index, len(self.certificates) - 1)
        return self.certificates[index]

    @property
    def text(self):
        return "\n".join(self.prompts)


@pytest.fixture
def operator(monkeypatch):
    """Install an :class:`Operator` as ``builtins.input``."""
    def install(**kwargs):
        person = Operator(**kwargs)
        monkeypatch.setattr("builtins.input", person)
        return person
    return install


@pytest.fixture(autouse=True)
def _no_signal_handler(monkeypatch):
    """Keep the app's SIGINT handler out of the test session.

    ``setup_graceful_shutdown`` installs a handler that calls ``sys.exit`` and
    never restores the previous one; leaving it behind would outlive the test.
    The interrupt paths are exercised by raising ``KeyboardInterrupt`` from a
    prompt, which is what a real Ctrl-C at a prompt does anyway.
    """
    monkeypatch.setattr(sc, "setup_graceful_shutdown", lambda cleanup: None)


# ── running the app ───────────────────────────────────────────────────

def invoke(tmp_path, *argv, dev=None, base="rec"):
    """Run the app with ``-o <tmp>/base`` and return its exit status."""
    args = sc.build_parser().parse_args(
        [*argv, "-o", os.path.join(str(tmp_path), base)])
    return sc.run(args, dev=dev)


def record_text(tmp_path, base="rec"):
    with open(os.path.join(str(tmp_path), f"{base}.log"), encoding="utf-8") as f:
        return f.read()


# ══════════════════════════════════════════════════════════════════════
# The dry run
# ══════════════════════════════════════════════════════════════════════

class TestDryRun:
    def test_it_sends_the_whole_sequence_and_never_stores(
            self, tmp_path, operator, capsys):
        dev, transport = opened()
        operator()
        status = invoke(tmp_path, "--artifact", "--ambient", "23.5", dev=dev)

        # Asserted against the wire, not against a printed message.
        assert mutating(transport) == ARTIFACT_COMMANDS[:-1]
        assert not any("CAL_STORE" in cmd for cmd in transport.log)
        assert status == sc.EXIT_OK

    def test_it_says_the_calibrator_is_on_uncommitted_constants(
            self, tmp_path, operator, capsys):
        dev, _ = opened()
        operator()
        invoke(tmp_path, "--artifact", "--ambient", "23.5", dev=dev)
        out = capsys.readouterr().out

        # CAL_ADJ really ran: the operator must not walk away believing the
        # instrument was untouched.
        assert "UNCOMMITTED CONSTANTS" in out
        assert "POWER-CYCLE" in out
        assert "--store" in out

    def test_the_record_names_what_was_not_sent(self, tmp_path, operator):
        dev, _ = opened()
        operator()
        invoke(tmp_path, "--artifact", "--ambient", "23.5", dev=dev)
        text = record_text(tmp_path)
        assert "NOT sent: CAL_STORE;*WAI" in text
        assert "DRY RUN" in text
        assert "UNTOUCHED" in text

    def test_it_is_the_default(self, tmp_path, operator):
        dev, transport = opened()
        operator()
        invoke(tmp_path, "--artifact", "--ambient", "23.5", dev=dev)
        assert "CAL_ADJ;*OPC" in transport.log
        assert not any("CAL_STORE" in cmd for cmd in transport.log)


# ══════════════════════════════════════════════════════════════════════
# The committing run
# ══════════════════════════════════════════════════════════════════════

class TestCommittingRun:
    def test_it_sends_the_full_sequence_including_the_store(
            self, tmp_path, operator):
        dev, transport = opened()
        operator()
        status = invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5",
                        dev=dev)
        assert mutating(transport) == ARTIFACT_COMMANDS
        assert status == sc.EXIT_OK

    def test_the_record_says_it_was_committed(self, tmp_path, operator):
        dev, _ = opened()
        operator()
        invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5", dev=dev)
        text = record_text(tmp_path)
        assert "COMMITTING RUN" in text
        assert "non-volatile       WRITTEN" in text
        assert "** COMMITTED" in text

    def test_the_typed_confirmation_is_still_required(self, tmp_path,
                                                      operator):
        dev, _ = opened()
        person = operator()
        invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5", dev=dev)
        typed = [p for p in person.prompts if "Override and accept" in p]
        assert typed, "no typed confirmation was asked for before CAL_STORE"
        assert any("NON-VOLATILE" in p for p in typed)
        assert all('type "yes"' in p for p in typed)

    def test_the_operator_is_sent_to_the_rear_switch(self, tmp_path, operator,
                                                     capsys):
        dev, _ = opened()
        operator()
        invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5", dev=dev)
        out = capsys.readouterr().out
        assert "GO TO THE BACK OF THE INSTRUMENT" in out
        assert "CALIBRATION switch to ENABLE" in out
        # ... and told to put it back, as a step of its own.
        assert "Set the rear CALIBRATION switch back to NORMAL." in out

    def test_the_certificates_and_their_uncertainties_are_in_the_record(
            self, tmp_path, operator):
        dev, _ = opened()
        operator()
        invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5", dev=dev)
        text = record_text(tmp_path)
        assert "10.00013" in text
        assert "1.50 ppm" in text                     # the 10 V uncertainty
        assert "4.00 ppm" in text                     # the 10 kOhm
        assert "10.00 ppm" in text                    # the 1 Ohm
        assert "inherits" in text


# ══════════════════════════════════════════════════════════════════════
# Declining the commit -- the run that is not a failure and not a success
# ══════════════════════════════════════════════════════════════════════

class TestDeclinedCommit:
    def test_cal_store_is_left_unsent(self, tmp_path, operator):
        dev, transport = opened()
        operator(explicit="no")
        status = invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5",
                        dev=dev)
        assert not any("CAL_STORE" in cmd for cmd in transport.log)
        assert status == sc.EXIT_FAIL

    def test_it_is_not_reported_as_a_success(self, tmp_path, operator, capsys):
        dev, _ = opened()
        operator(explicit="no")
        invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5", dev=dev)
        out = capsys.readouterr().out
        assert "** NOT COMMITTED" in out
        assert "** COMMITTED" not in out
        assert "UNCOMMITTED CONSTANTS" in out

    def test_the_app_does_not_read_success_from_result_failed(self):
        """The specific bug: ``failed`` is False for a declined commit.

        Driven through :func:`run_procedure` itself so the result is the one
        the framework really emits, then handed to the two functions the app
        decides with.
        """
        dev, transport = opened(**{"FAULT?": "0"})

        class Declining:
            """Says yes to everything except the typed commit confirmation."""

            def __init__(self):
                self.answers = list(CERTIFICATES)

            def show(self, banner, *body, panel_msg=None):
                pass

            def prompt(self, banner, *body, panel_msg=None, action=""):
                pass

            def confirm_ok(self, what="", *, hint=None):
                return True

            def confirm_explicit(self, what, *, hint=None):
                return False

            def ask(self, question):
                if "certified value" in question:
                    return self.answers[0][0]
                if "uncertainty" in question:
                    return self.answers.pop(0)[1]
                return ""

        procedure = get_procedure("fluke5700a", "artifact-cal")
        ctx = ProcedureContext(device=dev, console=Declining(), commit=True,
                               ambient_c=23.5)
        result = run_procedure(procedure, ctx)

        assert result.failed is False           # the trap
        assert result.committed is False
        assert result.nvram_uncertain is False
        assert not any("CAL_STORE" in cmd for cmd in transport.log)

        assert sc.sequence_complete(result) is False
        assert sc.exit_status(result) == sc.EXIT_FAIL
        rendered = "\n".join(sc.format_outcome(result, sc.Paths("x")))
        assert "NOT COMMITTED" in rendered

    def test_a_declined_required_step_stops_the_run(self, tmp_path, operator):
        dev, transport = opened()
        # Refusing the lead-reversal step: everything downstream of it is
        # blocked, exactly as fault 852 would block it.
        operator(ok_by_step={"ref-10v-neg": "n"})
        status = invoke(tmp_path, "--artifact", "--ambient", "23.5", dev=dev)
        assert not any("KOHM" in cmd for cmd in transport.log)
        assert not any("CAL_ADJ" in cmd for cmd in transport.log)
        assert status == sc.EXIT_FAIL


# ══════════════════════════════════════════════════════════════════════
# The three-valued NVRAM answer
# ══════════════════════════════════════════════════════════════════════

class TestNvramUnknown:
    def _run(self, tmp_path, operator, capsys):
        transport = FakeTransport(default_responses())
        transport.script("FAULT?", dead_after_store(transport))
        dev = Fluke5700A(transport=transport)
        operator()
        status = invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5",
                        dev=dev)
        return status, transport, capsys.readouterr().out

    def test_the_commit_really_went_out(self, tmp_path, operator, capsys):
        _, transport, _ = self._run(tmp_path, operator, capsys)
        assert "CAL_STORE;*WAI" in transport.log

    def test_it_is_rendered_as_neither_success_nor_failure(
            self, tmp_path, operator, capsys):
        status, _, out = self._run(tmp_path, operator, capsys)
        assert status == sc.EXIT_UNKNOWN
        assert status not in (sc.EXIT_OK, sc.EXIT_FAIL)
        assert "** COMMITTED" not in out
        assert "** NOT COMMITTED" not in out

    def test_it_is_loud_and_names_the_checks(self, tmp_path, operator, capsys):
        _, _, out = self._run(tmp_path, operator, capsys)
        assert "NON-VOLATILE CALIBRATION MEMORY IS UNKNOWN" in out
        assert "NEITHER a success NOR a failure" in out
        assert "CAL_DATE? CAL" in out
        assert "CAL_RPT?  CAL" in out
        assert "POSSIBLY ADJUSTED" in out

    def test_nothing_contradicts_it(self, tmp_path, operator, capsys):
        """The "nothing was stored" note must not appear next to it.

        That note asserts non-volatile memory is untouched, which is precisely
        the question this state leaves open.
        """
        _, _, out = self._run(tmp_path, operator, capsys)
        assert "UNCOMMITTED CONSTANTS" not in out
        assert "POWER-CYCLE it before using it" not in out
        assert "UNCOMMITTED CONSTANTS" not in record_text(tmp_path)
        # The only mention of nothing having been stored is the conditional
        # one inside the UNKNOWN block itself.
        assert "If it shows the previous date, nothing was stored" in out

    def test_the_record_says_unknown(self, tmp_path, operator, capsys):
        self._run(tmp_path, operator, capsys)
        text = record_text(tmp_path)
        assert "non-volatile       UNKNOWN" in text
        assert "UNKNOWN" in text

    def test_no_store_retry_is_offered(self, tmp_path, operator, capsys):
        """Sending another irreversible command on top of one nobody can
        account for is not a recovery -- reading ``CAL_DATE?`` is."""
        _, transport, out = self._run(tmp_path, operator, capsys)
        assert sum(1 for cmd in transport.log
                   if cmd.startswith("CAL_STORE")) == 1
        assert "retried on its own" not in out


# ══════════════════════════════════════════════════════════════════════
# The CALIBRATION switch, and the single-step retry
# ══════════════════════════════════════════════════════════════════════

class TestSwitchLockout:
    def test_fault_845_is_explained_and_nothing_is_stored(
            self, tmp_path, operator, capsys):
        transport = FakeTransport(default_responses())
        transport.script("FAULT?", FaultScript(transport, lockouts=99))
        dev = Fluke5700A(transport=transport)
        operator(retry="n")                  # decline the retry offer
        status = invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5",
                        dev=dev)
        out = capsys.readouterr().out
        assert "was REFUSED (fault 845)" in out
        assert "NOTHING WAS STORED" in out
        assert "rear-panel CALIBRATION switch is in the NORMAL position" in out
        assert status == sc.EXIT_FAIL
        assert sum(1 for cmd in transport.log
                   if cmd.startswith("CAL_STORE")) == 1

    def test_the_retry_commits_without_repeating_the_sequence(
            self, tmp_path, operator, capsys):
        transport = FakeTransport(default_responses())
        transport.script("FAULT?", FaultScript(transport, lockouts=1))
        dev = Fluke5700A(transport=transport)
        operator()
        status = invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5",
                        dev=dev)
        out = capsys.readouterr().out

        stores = [cmd for cmd in transport.log if cmd.startswith("CAL_STORE")]
        assert len(stores) == 2, "the retry did not resend CAL_STORE"
        # ... and the four CAL_REF steps were NOT repeated.
        assert sum(1 for cmd in transport.log
                   if cmd.startswith("CAL_REF")) == 4
        assert sum(1 for cmd in transport.log
                   if cmd.startswith("CAL_ADJ")) == 1
        assert status == sc.EXIT_OK
        assert "** COMMITTED" in out
        assert "non-volatile       WRITTEN" in record_text(tmp_path)

    def test_a_refused_retry_is_recorded_as_a_command_that_was_sent(
            self, tmp_path, operator, monkeypatch):
        """Fault 845 means the instrument *answered* CAL_STORE, not that it
        never left.

        `cal.procedure.classify_failure` is the single implementation of that
        distinction, and this asserts the app routes its own retry through it:
        the refused command belongs in ``commands``, not in
        ``skipped_commands`` — while non-volatile memory stays known-UNTOUCHED,
        because a queued 845 is the calibrator saying so.
        """
        transport = FakeTransport(default_responses())
        transport.script("FAULT?", FaultScript(transport, lockouts=99))
        dev = Fluke5700A(transport=transport)
        operator()          # retries three times, refused every time

        recorded = []
        real_record = ProcedureContext.record

        def capture(self, result):
            recorded.append(result)
            return real_record(self, result)

        monkeypatch.setattr(ProcedureContext, "record", capture)
        status = invoke(tmp_path, "--artifact", "--store", "--ambient",
                        "23.5", dev=dev)

        retries = [r for r in recorded if r.step_id == "store-retry"]
        assert retries, "the refused retry left no row in the audit trail"
        for row in retries:
            assert row.commands == ("CAL_STORE;*WAI",)
            assert row.skipped_commands == ()
            assert row.nvram is NvramState.UNTOUCHED
            assert row.committed is False
            assert row.nvram_uncertain is False
        assert status == sc.EXIT_FAIL

        # And what the framework decides for the same failure is what the app
        # recorded -- one implementation, not two.
        expected = classify_failure(
            CalibrationSwitchLockout("CAL_STORE", (845,)),
            ("CAL_STORE;*WAI",), writes_nvram=True)
        assert expected == (NvramState.UNTOUCHED, ("CAL_STORE;*WAI",), ())

    def test_an_interrupt_during_the_retry_is_recorded_as_unknown(
            self, tmp_path, operator, capsys, monkeypatch):
        """Ctrl-C with ``CAL_STORE`` already on its way out.

        The command left; the answer never came. Recording that as "the
        operator cancelled it" would claim to know what nobody knows.
        """
        transport = FakeTransport(default_responses())
        transport.script("FAULT?", FaultScript(transport, lockouts=1))
        dev = Fluke5700A(transport=transport)
        operator()

        procedure = get_procedure("fluke5700a", "artifact-cal")
        real_store = procedure.store_step
        calls = {"n": 0}

        def flaky_store(ctx, *, record=True):
            # The first call is the runner's own store step: let it fail on
            # fault 845 for real, so the retry is offered the way it is in the
            # field. The Ctrl-C lands on the retry.
            calls["n"] += 1
            if calls["n"] == 1:
                return real_store(ctx, record=record)
            raise KeyboardInterrupt

        monkeypatch.setattr(procedure, "store_step", flaky_store,
                            raising=True)
        status = invoke(tmp_path, "--artifact", "--store", "--ambient",
                        "23.5", dev=dev)

        out = capsys.readouterr().out
        assert status == sc.EXIT_UNKNOWN
        assert "NON-VOLATILE CALIBRATION MEMORY IS UNKNOWN" in out
        assert "store-retry" in record_text(tmp_path)

    def test_only_the_final_verdict_is_printed(self, tmp_path, operator,
                                               capsys):
        """One verdict, from the result that stands.

        Printing "NOT COMMITTED -- power-cycle it" and then committing thirty
        seconds later would leave two contradictory answers on the screen with
        nothing to say which one is the run's.
        """
        transport = FakeTransport(default_responses())
        transport.script("FAULT?", FaultScript(transport, lockouts=1))
        dev = Fluke5700A(transport=transport)
        operator()
        invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5", dev=dev)
        out = capsys.readouterr().out
        assert out.count("** COMMITTED") == 1
        assert "** NOT COMMITTED" not in out
        assert "UNCOMMITTED CONSTANTS" not in out


# ══════════════════════════════════════════════════════════════════════
# The record could not be written where it was asked to go
# ══════════════════════════════════════════════════════════════════════

class TestRecordLoss:
    """A committed run whose record did not land must not exit 0.

    The pre-flight probe makes this nearly unreachable, so this is the
    belt-and-braces half: the disk fills, or the share goes away, *during* the
    hour the calibration takes.
    """

    def _run(self, tmp_path, operator, capsys, monkeypatch):
        # ``_fallback_record`` writes to the system temp directory by design --
        # that is the point of a last-resort record.  Two of the four tests
        # below do not consume the path it prints, so each suite run left two
        # session logs in /tmp forever; 108 had accumulated locally.  Point the
        # default at tmp_path, which pytest reaps, without changing what the
        # code under test does.
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        base = os.path.join(str(tmp_path), "rec")
        transport = FakeTransport(default_responses())
        transport.script("FAULT?",
                         sabotage_log_after_store(transport, base + ".log"))
        dev = Fluke5700A(transport=transport)
        operator()
        args = sc.build_parser().parse_args(
            ["--artifact", "--store", "--ambient", "23.5", "-o", base])
        status = sc.run(args, dev=dev)
        return status, transport, capsys.readouterr().out

    @staticmethod
    def _fallback_path(out):
        for line in out.split("\n"):
            if "It was saved to " in line:
                return line.split("It was saved to ", 1)[1].split(" instead")[0]
        return ""

    def test_it_does_not_exit_ok(self, tmp_path, operator, capsys,
                                 monkeypatch):
        status, transport, out = self._run(tmp_path, operator, capsys, monkeypatch)
        # The calibration really was committed ...
        assert "CAL_STORE;*WAI" in transport.log
        assert "** COMMITTED" in out
        # ... and the session still must not report success.
        assert status == sc.EXIT_NO_RECORD
        assert status != sc.EXIT_OK

    def test_it_says_so_loudly(self, tmp_path, operator, capsys,
                               monkeypatch):
        _, _, out = self._run(tmp_path, operator, capsys, monkeypatch)
        assert "THE SESSION LOG COULD NOT BE WRITTEN" in out
        assert "COPY IT SOMEWHERE PERMANENT NOW" in out

    def test_the_record_lands_somewhere(self, tmp_path, operator, capsys,
                                        monkeypatch):
        _, _, out = self._run(tmp_path, operator, capsys, monkeypatch)
        path = self._fallback_path(out)
        assert path, "no fallback path was printed"
        try:
            with open(path, encoding="utf-8") as handle:
                text = handle.read()
            assert "session record" in text
            assert "non-volatile       WRITTEN" in text
            assert "CAL_STORE;*WAI" in text
        finally:
            os.unlink(path)

    def test_the_other_files_are_still_written(self, tmp_path, operator,
                                               capsys, monkeypatch):
        """One failed write must not take the rest of the evidence with it."""
        _, _, out = self._run(tmp_path, operator, capsys, monkeypatch)
        try:
            assert os.path.exists(os.path.join(str(tmp_path),
                                               "rec-before.txt"))
            assert os.path.exists(os.path.join(str(tmp_path), "rec-after.txt"))
        finally:
            path = self._fallback_path(out)
            if path and os.path.exists(path):
                os.unlink(path)


# ══════════════════════════════════════════════════════════════════════
# Interruption -- the record survives
# ══════════════════════════════════════════════════════════════════════

class TestInterrupt:
    def test_the_record_is_written_with_the_completed_steps(
            self, tmp_path, operator):
        dev, transport = opened()
        # Ctrl-C at the CAL_ADJ step's confirmation, after all four references.
        operator(interrupt_on="step adjust?")
        status = invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5",
                        dev=dev)

        assert status == sc.EXIT_ABORTED
        text = record_text(tmp_path)
        assert "ref-10v-pos: ok" in text
        assert "ref-1ohm: ok" in text
        assert "INTERRUPTED" in text
        assert "never reached" in text
        assert not any("CAL_ADJ" in cmd for cmd in transport.log)
        assert not any("CAL_STORE" in cmd for cmd in transport.log)

    def test_the_record_survives_an_interrupt_that_escapes_the_runner(
            self, tmp_path, operator):
        """Ctrl-C while typing a certificate, before the step loop exists.

        ``run_procedure``'s deferred SIGINT covers the steps; this one is
        raised out of ``prepare`` and propagates, so the app has to build the
        record itself -- through the procedure's own ``finalize``.
        """
        dev, transport = opened()
        operator(interrupt_on="expanded uncertainty")
        status = invoke(tmp_path, "--artifact", "--ambient", "23.5", dev=dev)

        assert status == sc.EXIT_ABORTED
        assert os.path.exists(os.path.join(str(tmp_path), "rec.log"))
        text = record_text(tmp_path)
        assert "interrupted" in text.lower()
        assert not mutating(transport)

    def test_an_interrupt_with_no_record_at_all_exits_no_record(
            self, tmp_path, operator, capsys, monkeypatch):
        """The last-resort branch: no result *and* the log will not write.

        Two independent failures at once, which is why nothing reached it: the
        procedure's ``finalize`` raises (so the app has no
        :class:`~caldmm.cal.procedure.ProcedureResult` to render) and the path
        the pre-flight proved writable has stopped being writable.  All that is
        left is the raw step log, and if that does not land either the session
        must not report a plain abort -- exit 3 tells a wrapper script "nothing
        to see, the record is where you asked"; here there is no record.

        ``finalize`` is faulted through the *registered procedure*, not through
        the app: the app under test is still the real one, running its real
        exception path.
        """
        monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
        log_path = os.path.join(str(tmp_path), "rec.log")

        procedure = get_procedure("fluke5700a", "artifact-cal")

        def explode(ctx):
            raise RuntimeError("the record could not be assembled")

        monkeypatch.setattr(procedure, "finalize", explode)

        class Saboteur(Operator):
            def __call__(self, prompt=""):
                if "expanded uncertainty" in prompt:
                    # The disk fills / the share goes away, after the
                    # pre-flight writability probe has already passed.
                    os.mkdir(log_path)
                    raise KeyboardInterrupt
                return super().__call__(prompt)

        monkeypatch.setattr("builtins.input", Saboteur())
        dev, transport = opened()
        status = invoke(tmp_path, "--artifact", "--ambient", "23.5", dev=dev)

        assert status == sc.EXIT_NO_RECORD
        assert status != sc.EXIT_ABORTED
        assert os.path.isdir(log_path), "the log path stayed unwritable"
        assert not mutating(transport), "nothing may have been sent"
        assert "the raw step log went to" in capsys.readouterr().out

    def test_nothing_is_committed_by_an_interrupted_run(self, tmp_path,
                                                        operator, capsys):
        dev, _ = opened()
        operator(interrupt_on="step adjust?")
        invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5", dev=dev)
        out = capsys.readouterr().out
        assert "NOTHING WAS COMMITTED" in out
        assert "** COMMITTED" not in out


# ══════════════════════════════════════════════════════════════════════
# Refusals before anything is sent
# ══════════════════════════════════════════════════════════════════════

class TestRefusals:
    def test_a_decade_error_is_rejected_before_anything_is_sent(
            self, tmp_path, operator, capsys):
        dev, transport = opened()
        # 10 V typed as 10000 -- a units mistake, six orders of magnitude out.
        operator(certificates=[("10000", "1.5 ppm")])
        status = invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5",
                        dev=dev)
        out = capsys.readouterr().out

        assert status == sc.EXIT_USAGE
        assert not mutating(transport)
        assert "decade" in out
        assert "CAL_REF" in out          # it says what it refused to send to

    def test_an_ambient_outside_the_specified_band_is_refused(
            self, tmp_path, operator, capsys):
        dev, transport = opened()
        operator()
        status = invoke(tmp_path, "--artifact", "--ambient", "40", dev=dev)
        assert status == sc.EXIT_USAGE
        assert not mutating(transport)
        assert "15-35" in capsys.readouterr().err

    def test_a_bad_ambient_is_refused_before_the_instrument_is_touched(
            self, tmp_path, capsys):
        """No ``dev``: it must not get as far as opening one."""
        args = sc.build_parser().parse_args(
            ["--artifact", "--ambient", "not a number",
             "-o", os.path.join(str(tmp_path), "rec")])
        assert sc.run(args) == sc.EXIT_USAGE
        assert "--ambient" in capsys.readouterr().err
        assert not os.path.exists(os.path.join(str(tmp_path), "rec.log"))

    def test_a_cold_calibrator_blocks_a_committing_run(self, tmp_path,
                                                       operator, capsys):
        dev, transport = opened(**{"ONTIME?": "3"})
        operator()
        status = invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5",
                        dev=dev)
        out = capsys.readouterr().out
        assert status == sc.EXIT_USAGE
        assert not mutating(transport)
        assert "warm" in out
        assert "--force does not override these" in out

    def test_a_live_output_above_22_v_is_refused(self, tmp_path, operator,
                                                 capsys):
        dev, transport = opened(**{"OUT?": "1.0E+03,V,0"})
        operator()
        status = invoke(tmp_path, "--artifact", "--ambient", "23.5", dev=dev)
        out = capsys.readouterr().out
        assert status == sc.EXIT_USAGE
        assert not mutating(transport)
        assert "22 V" in out
        assert "no remote abort" in out

    def test_an_unwritable_output_path_is_refused_before_step_1(
            self, tmp_path, operator, capsys):
        """The record must have somewhere to go *before* the sequence runs.

        Refusing to overwrite is not the same check as being able to write, and
        a typo'd ``-o`` passes the first one trivially. Without this the whole
        irreversible sequence ran, CAL_STORE went out, and the write failed
        with the calibration committed and its only account on a terminal.
        """
        dev, transport = opened()
        operator()
        # A path under a *file*, so the directory can neither exist nor be
        # created -- the shape a typo or an unmounted share really takes.
        blocker = os.path.join(str(tmp_path), "not-a-dir")
        with open(blocker, "w") as handle:
            handle.write("")
        args = sc.build_parser().parse_args(
            ["--artifact", "--store", "--ambient", "23.5",
             "-o", os.path.join(blocker, "run")])
        status = sc.run(args, dev=dev)

        assert status == sc.EXIT_USAGE
        assert not mutating(transport)
        assert not any("CAL_STORE" in cmd for cmd in transport.log)
        assert "Nothing has been sent to the calibrator" in \
            capsys.readouterr().err

    def test_the_probe_creates_a_missing_directory(self, tmp_path, operator):
        """``-o runs/2026-08-02/artifact`` is a reasonable thing to type."""
        dev, _ = opened()
        operator()
        base = os.path.join(str(tmp_path), "runs", "2026-08-02", "artifact")
        args = sc.build_parser().parse_args(
            ["--artifact", "--ambient", "23.5", "-o", base])
        assert sc.run(args, dev=dev) == sc.EXIT_OK
        assert os.path.exists(base + ".log")

    def test_the_probe_leaves_no_files_behind_when_it_refuses(
            self, tmp_path, operator, capsys):
        dev, _ = opened()
        operator()
        # The log probes fine; the second path does not, because a directory
        # is in the way of the file the app would write.
        base = os.path.join(str(tmp_path), "rec")
        os.mkdir(base + "-before.txt")
        args = sc.build_parser().parse_args(
            ["--artifact", "--ambient", "23.5", "-o", base])
        assert sc.run(args, dev=dev) == sc.EXIT_USAGE
        # The probe created rec.log to test it, and removed it again.
        assert not os.path.exists(base + ".log")

    def test_an_existing_record_is_not_overwritten(self, tmp_path, operator):
        dev, transport = opened()
        operator()
        with open(os.path.join(str(tmp_path), "rec.log"), "w") as handle:
            handle.write("an earlier calibration")
        status = invoke(tmp_path, "--artifact", "--ambient", "23.5", dev=dev)
        assert status == sc.EXIT_USAGE
        assert not mutating(transport)
        assert record_text(tmp_path) == "an earlier calibration"

    def test_force_overwrites_it(self, tmp_path, operator):
        dev, _ = opened()
        operator()
        with open(os.path.join(str(tmp_path), "rec.log"), "w") as handle:
            handle.write("an earlier calibration")
        status = invoke(tmp_path, "--artifact", "--ambient", "23.5", "--force",
                        dev=dev)
        assert status == sc.EXIT_OK
        assert "session record" in record_text(tmp_path)

    def test_a_mode_is_required(self, tmp_path, capsys):
        status = invoke(tmp_path)
        assert status == sc.EXIT_USAGE
        assert "required" in capsys.readouterr().err

    def test_the_modes_are_mutually_exclusive(self, tmp_path, capsys):
        status = invoke(tmp_path, "--zeros", "--artifact")
        assert status == sc.EXIT_USAGE
        assert "mutually exclusive" in capsys.readouterr().err


# ══════════════════════════════════════════════════════════════════════
# The before/after evidence
# ══════════════════════════════════════════════════════════════════════

class TestReportsAndDiff:
    def test_before_and_after_are_captured_saved_and_diffed(
            self, tmp_path, operator, capsys):
        dev, _ = opened()
        operator()
        invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5", dev=dev)
        out = capsys.readouterr().out

        before = os.path.join(str(tmp_path), "rec-before.txt")
        after = os.path.join(str(tmp_path), "rec-after.txt")
        diff = os.path.join(str(tmp_path), "rec-diff.csv")
        assert os.path.exists(before) and os.path.exists(after)
        assert os.path.exists(diff)

        with open(before) as handle:
            before_text = handle.read()
        with open(after) as handle:
            after_text = handle.read()
        assert "-18.88" in before_text          # captured verbatim
        assert "-2.10" in after_text
        assert before_text != after_text

        assert "Report diff" in out
        assert "before %" in out

    def test_the_after_capture_happens_before_the_commit(self, tmp_path,
                                                         operator):
        """The report the store freezes is the one that was captured."""
        dev, transport = opened()
        operator()
        invoke(tmp_path, "--artifact", "--store", "--ambient", "23.5", dev=dev)
        captures = [i for i, cmd in enumerate(transport.log)
                    if cmd == "CAL_RPT? CAL"]
        store = transport.log.index("CAL_STORE;*WAI")
        assert len(captures) == 2
        assert captures[1] < store

    def test_the_diff_csv_carries_both_sides(self, tmp_path, operator):
        import csv as _csv
        dev, _ = opened()
        operator()
        invoke(tmp_path, "--artifact", "--ambient", "23.5", dev=dev)
        with open(os.path.join(str(tmp_path), "rec-diff.csv")) as handle:
            rows = list(_csv.DictReader(handle))
        assert rows
        assert set(rows[0]) == set(sc.DIFF_CSV_COLUMNS)
        first = rows[0]
        assert first["status"] == "both"
        assert float(first["before_percent_of_spec"]) == pytest.approx(-18.88)
        assert float(first["after_percent_of_spec"]) == pytest.approx(-2.10)
        assert float(first["delta_percent_of_spec"]) == pytest.approx(16.78)


# ══════════════════════════════════════════════════════════════════════
# The other procedures
# ══════════════════════════════════════════════════════════════════════

class TestZeros:
    def test_it_runs_cal_zero_and_writes_a_record(self, tmp_path, operator,
                                                  capsys):
        dev, transport = opened()
        operator()
        status = invoke(tmp_path, "--zeros", dev=dev)
        assert mutating(transport) == ["CAL_ZERO;*OPC"]
        assert status == sc.EXIT_OK
        text = record_text(tmp_path)
        assert "dc zeros" in text
        assert "non-volatile       WRITTEN" in text

    def test_it_still_asks_for_a_typed_confirmation(self, tmp_path, operator):
        dev, _ = opened()
        person = operator()
        invoke(tmp_path, "--zeros", dev=dev)
        assert any("Override and accept" in p for p in person.prompts)

    def test_declining_it_sends_nothing(self, tmp_path, operator):
        dev, transport = opened()
        operator(explicit="no")
        status = invoke(tmp_path, "--zeros", dev=dev)
        assert not any("CAL_ZERO" in cmd for cmd in transport.log)
        assert status == sc.EXIT_FAIL


class TestCheck:
    def test_it_runs_cal_chk_and_writes_a_record(self, tmp_path, operator):
        dev, transport = opened()
        operator()
        status = invoke(tmp_path, "--check", "--ambient", "23.5", dev=dev)
        assert mutating(transport) == ["CAL_TEMP 23.5", "CAL_CHK;*OPC"]
        assert status == sc.EXIT_OK
        text = record_text(tmp_path)
        assert "calibration check" in text
        assert "non-volatile       UNTOUCHED" in text

    def test_it_never_stores(self, tmp_path, operator, capsys):
        dev, transport = opened()
        operator()
        invoke(tmp_path, "--check", "--ambient", "23.5", "--store", dev=dev)
        assert not any("CAL_STORE" in cmd for cmd in transport.log)
        assert "nothing to commit" in capsys.readouterr().out

    def test_the_previous_check_report_is_captured_first(self, tmp_path,
                                                         operator):
        dev, transport = opened()
        operator()
        invoke(tmp_path, "--check", "--ambient", "23.5", dev=dev)
        captures = [i for i, cmd in enumerate(transport.log)
                    if cmd == "CAL_RPT? CHECK"]
        check = transport.log.index("CAL_CHK;*OPC")
        assert captures[0] < check < captures[1]

    def test_it_asks_for_the_ambient_when_none_is_given(self, tmp_path,
                                                        operator):
        dev, transport = opened()
        person = operator(ambient="24.2")
        invoke(tmp_path, "--check", dev=dev)
        assert any("ambient temperature" in p for p in person.prompts)
        assert "CAL_TEMP 24.2" in transport.log


# ══════════════════════════════════════════════════════════════════════
# --report and --compare
# ══════════════════════════════════════════════════════════════════════

class TestReportCapture:
    def test_it_sends_no_calibration_command(self, tmp_path, capsys):
        """Not the same claim as "changes nothing".

        ``mutating()`` filters ``CAL_*`` only, and that is all this asserts:
        opening the session runs the guide §14.1 sequence, which includes
        ``*RST`` and ``STBY``, so a live output *is* dropped. The name used to
        overstate what the assertion checks.
        """
        dev, transport = opened()
        status = invoke(tmp_path, "--report", "CAL", dev=dev)
        assert status == sc.EXIT_OK
        assert not mutating(transport)
        assert "*RST" in transport.log and "STBY" in transport.log
        with open(os.path.join(str(tmp_path), "rec.txt")) as handle:
            assert "OUTPUT CALIBRATION" in handle.read()
        assert "worst" in capsys.readouterr().out

    def test_it_writes_the_shift_table_as_csv(self, tmp_path):
        dev, _ = opened()
        invoke(tmp_path, "--report", "CAL", dev=dev)
        assert os.path.exists(os.path.join(str(tmp_path), "rec.csv"))

    def test_an_unknown_group_is_refused(self, tmp_path, capsys):
        status = invoke(tmp_path, "--report", "EVERYTHING")
        assert status == sc.EXIT_USAGE
        assert "CAL, CHECK, RAW" in capsys.readouterr().err


class TestCompare:
    def _files(self, tmp_path):
        before = os.path.join(str(tmp_path), "a.txt")
        after = os.path.join(str(tmp_path), "b.txt")
        with open(before, "w") as handle:
            handle.write(BEFORE_REPORT)
        with open(after, "w") as handle:
            handle.write(AFTER_REPORT)
        return before, after

    def test_it_needs_no_instrument(self, tmp_path, capsys):
        before, after = self._files(tmp_path)
        args = sc.build_parser().parse_args(["--compare", before, after])
        assert sc.run(args) == sc.EXIT_OK
        out = capsys.readouterr().out
        assert "2 points compared" in out
        assert "-18.88" in out and "-2.10" in out

    def test_it_writes_a_csv_when_asked(self, tmp_path):
        import csv as _csv
        before, after = self._files(tmp_path)
        status = invoke(tmp_path, "--compare", before, after)
        assert status == sc.EXIT_OK
        with open(os.path.join(str(tmp_path), "rec-diff.csv")) as handle:
            rows = list(_csv.DictReader(handle))
        assert len(rows) == 2
        assert all(row["status"] == "both" for row in rows)

    def test_a_file_with_no_report_is_refused(self, tmp_path, capsys):
        junk = os.path.join(str(tmp_path), "junk.txt")
        with open(junk, "w") as handle:
            handle.write("this is not a calibration report")
        args = sc.build_parser().parse_args(["--compare", junk, junk])
        assert sc.run(args) == sc.EXIT_USAGE
        assert "no 5700A calibration report" in capsys.readouterr().err


    def test_two_different_instruments_are_called_out(self, tmp_path, capsys):
        before, after = self._files(tmp_path)
        other = os.path.join(str(tmp_path), "other.txt")
        with open(other, "w") as handle:
            handle.write(AFTER_REPORT.replace("5248000", "9999999"))
        args = sc.build_parser().parse_args(["--compare", before, other])
        sc.run(args)
        assert "DIFFERENT instruments" in capsys.readouterr().out


# ══════════════════════════════════════════════════════════════════════
# The functions the verdict is decided with
# ══════════════════════════════════════════════════════════════════════

class TestVerdictHelpers:
    def _result(self, *, commit, **overrides):
        dev, _ = opened(**overrides)

        class Yes:
            def __init__(self):
                self.answers = list(CERTIFICATES)

            def show(self, banner, *body, panel_msg=None):
                pass

            def prompt(self, banner, *body, panel_msg=None, action=""):
                pass

            def confirm_ok(self, what="", *, hint=None):
                return True

            def confirm_explicit(self, what, *, hint=None):
                return True

            def ask(self, question):
                if "certified value" in question:
                    return self.answers[0][0]
                if "uncertainty" in question:
                    return self.answers.pop(0)[1]
                return ""

        procedure = get_procedure("fluke5700a", "artifact-cal")
        ctx = ProcedureContext(device=dev, console=Yes(), commit=commit,
                               ambient_c=23.5)
        return run_procedure(procedure, ctx)

    def test_a_finished_dry_run_is_complete_here_but_not_there(self):
        """``ProcedureResult.complete`` is False for every dry run by design.

        Its NVRAM steps are ``SKIPPED_DRY_RUN``, which is the outcome that was
        asked for, so the app counts them as having run and nothing else.
        """
        result = self._result(commit=False)
        assert result.complete is False
        assert sc.sequence_complete(result) is True
        assert sc.exit_status(result) == sc.EXIT_OK
        assert any(step.outcome is StepOutcome.SKIPPED_DRY_RUN
                   for step in result.steps)

    def test_a_finished_committing_run_is_complete_both_ways(self):
        result = self._result(commit=True)
        assert result.complete is True
        assert sc.sequence_complete(result) is True
        assert result.committed is True
        assert sc.exit_status(result) == sc.EXIT_OK

    def test_the_uncommitted_note_fires_when_cal_adj_has_run(self):
        result = self._result(commit=False)
        note = "\n".join(sc._uncommitted_note(result))
        assert "UNCOMMITTED CONSTANTS" in note
        assert "POWER-CYCLE" in note


class TestOneClockPerRecord:
    """A record with two clocks in it is a record an auditor cannot read.

    ``format_record`` stamps "written" with the local-aware clock; the
    "started / finished" pair comes from ``ProcedureResult``, which
    ``cal.procedure._now`` stamps.  While that one was UTC, the permanent
    account of an irreversible operation said ``written 12:57:11-07:00`` and
    ``started 19:57:11+00:00`` -- the same instant, printed as though the run
    had begun seven hours after it was written up.
    """

    def test_written_and_started_carry_the_same_offset(self, tmp_path,
                                                       operator):
        import re
        from datetime import datetime

        dev, _ = opened()
        operator()
        invoke(tmp_path, "--artifact", "--ambient", "23.5", dev=dev)
        text = record_text(tmp_path)

        offsets = {}
        for label, pattern in (("written", r"^written\s+(\S+ \S+)"),
                               ("started", r"^started / finished\s+(\S+ \S+)")):
            match = re.search(pattern, text, re.MULTILINE)
            assert match, f"no {label} stamp in the record:\n{text[:400]}"
            offsets[label] = datetime.fromisoformat(match.group(1)).utcoffset()

        assert offsets["written"] is not None, "the stamps must stay aware"
        assert offsets["written"] == offsets["started"]
        assert offsets["written"] == datetime.now().astimezone().utcoffset()


class TestExitStatusContract:
    """The numbers themselves, pinned.

    Every other test here spells its expectation ``sc.EXIT_OK``, so the whole
    suite stayed green with ``EXIT_OK = 99``.  The howto publishes 0-5 as a
    scripting contract, and a wrapper script compares against literals.
    """

    def test_the_numbers_are_the_published_ones(self):
        assert sc.EXIT_OK == 0
        assert sc.EXIT_FAIL == 1
        assert sc.EXIT_USAGE == 2
        assert sc.EXIT_ABORTED == 3
        assert sc.EXIT_UNKNOWN == 4
        assert sc.EXIT_NO_RECORD == 5


# ══════════════════════════════════════════════════════════════════════
# The console
# ══════════════════════════════════════════════════════════════════════

class TestTranscriptConsole:
    def test_it_is_a_real_operator_console(self):
        from caldmm.lib.console import OperatorConsole
        assert issubclass(sc.TranscriptConsole, OperatorConsole)

    def test_it_records_what_was_asked_and_answered(self, monkeypatch):
        console = sc.TranscriptConsole()
        monkeypatch.setattr("builtins.input", lambda prompt="": "10.00013")
        assert console.ask("certified value: ") == "10.00013"
        assert any("10.00013" in line for line in console.transcript)

    def test_it_records_a_declined_confirmation(self, monkeypatch):
        console = sc.TranscriptConsole()
        monkeypatch.setattr("builtins.input", lambda prompt="": "no")
        assert console.confirm_explicit("committing") is False
        assert any("-> NO" in line for line in console.transcript)

    def test_a_prompted_step_is_recorded_once(self, monkeypatch):
        """``OperatorConsole.prompt`` renders through ``self.show``.

        Recording in both put every operator-attended step in the record
        twice, which in a record whose job is to say what happened reads as
        the step having been taken twice.
        """
        console = sc.TranscriptConsole()
        monkeypatch.setattr("builtins.input", lambda prompt="": "")
        console.prompt("── step 4/10: ref-10v-pos ──", "Connect the 10 V")
        banners = [line for line in console.transcript
                   if line.startswith("── step")]
        assert banners == ["── step 4/10: ref-10v-pos ──"]
        assert sum(1 for line in console.transcript
                   if "Connect the 10 V" in line) == 1

    def test_the_transcript_reaches_the_record(self, tmp_path, operator):
        dev, _ = opened()
        operator()
        invoke(tmp_path, "--artifact", "--ambient", "23.5", dev=dev)
        text = record_text(tmp_path)
        assert "Operator transcript" in text
        assert "[ask]" in text
        assert text.count("── step 8/10: adjust ──") == 1


# ── interface selection (GPIB or RS-232C) ─────────────────────────────

class TestInterfaceSelection:
    """``--port`` opens the rear-panel serial port instead of GPIB."""

    def _opened_with(self, tmp_path, argv):
        """Parse *argv*, run, and report how the session was opened."""
        args = sc.build_parser().parse_args(
            [*argv, "-o", os.path.join(str(tmp_path), "rec")])
        with patch.object(sc, "Fluke5700A",
                          side_effect=OSError("stop here")) as driver:
            assert sc.run(args) == sc.EXIT_USAGE
        return driver.call_args

    def test_the_default_is_still_gpib(self, tmp_path):
        call = self._opened_with(tmp_path, ["--report"])
        assert call.args == (sc.f57.DEFAULT_GPIB_ADDRESS,
                             sc.f57.DEFAULT_GPIB_BUS)
        assert "serial_port" not in call.kwargs

    def test_port_and_baud_open_a_serial_session(self, tmp_path):
        call = self._opened_with(
            tmp_path,
            ["--report", "--port", "/dev/ttyUSB0", "--baud", "19200"])
        assert call.kwargs["serial_port"] == "/dev/ttyUSB0"
        assert call.kwargs["serial_baud"] == 19200

    def test_a_failed_open_names_the_serial_port(self, tmp_path, capsys):
        self._opened_with(tmp_path, ["--report", "--port", "/dev/ttyUSB0"])
        assert "serial /dev/ttyUSB0 at 9600 baud" in capsys.readouterr().err

    def test_an_address_and_a_port_are_mutually_exclusive(self):
        with pytest.raises(SystemExit) as excinfo:
            sc.build_parser().parse_args(
                ["--report", "--address", "4", "--port", "/dev/ttyUSB0"])
        assert excinfo.value.code == 2

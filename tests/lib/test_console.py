"""Tests for caldmm.lib.console."""

import pytest

from caldmm.lib.console import OperatorConsole, PANEL_W


class TestShow:
    def test_output_formatting(self, capsys):
        console = OperatorConsole()
        console.show('── BANNER ──', 'line one', '', 'line two')
        out = capsys.readouterr().out
        lines = out.split('\n')
        # Leading blank line, then the banner verbatim.
        assert lines[0] == ''
        assert lines[1] == '── BANNER ──'
        # Non-empty body lines are indented two spaces...
        assert lines[2] == '  line one'
        # ...but an intentionally blank body line stays genuinely empty,
        # not two spaces.
        assert lines[3] == ''
        assert lines[4] == '  line two'

    def test_no_panel_by_default(self):
        # No panel wired -> show() must not raise even though there is
        # nothing to push to.
        console = OperatorConsole()
        console.show('banner')

    def test_panel_called_with_truncated_string(self):
        calls = []
        console = OperatorConsole(panel=calls.append)
        console.show('banner', panel_msg='this string is definitely longer than sixteen chars')
        assert len(calls) == 1
        assert calls[0] == 'this string is d'
        assert len(calls[0]) == PANEL_W

    def test_panel_not_called_when_none(self, capsys):
        console = OperatorConsole(panel=None)
        console.show('banner', panel_msg='whatever')
        # No exception, and stdout still has the banner.
        out = capsys.readouterr().out
        assert 'banner' in out

    def test_panel_exception_does_not_propagate_or_suppress_output(self, capsys):
        def boom(msg):
            raise RuntimeError('front panel is dead')

        console = OperatorConsole(panel=boom)
        # Must not raise.
        console.show('banner text', 'body line')
        out = capsys.readouterr().out
        assert 'banner text' in out
        assert '  body line' in out

    def test_panel_default_falls_back_to_stripped_banner(self):
        calls = []
        console = OperatorConsole(panel=calls.append)
        console.show('── PHASE 1 · SETUP ─────────────────────────────────')
        assert len(calls) == 1
        assert calls[0] == 'PHASE 1 · SETUP'[:PANEL_W]
        assert len(calls[0]) <= PANEL_W


class TestPrompt:
    def test_shows_then_blocks_on_input(self, capsys, monkeypatch):
        seen_prompts = []

        def fake_input(prompt=''):
            seen_prompts.append(prompt)
            return ''

        monkeypatch.setattr('builtins.input', fake_input)
        console = OperatorConsole()
        console.prompt('banner', 'body')
        out = capsys.readouterr().out
        assert 'banner' in out
        assert '  body' in out
        assert seen_prompts == [
            '  → press Enter when the action above is complete (Ctrl-C to abort): '
        ]

    def test_custom_action(self, monkeypatch):
        seen_prompts = []
        monkeypatch.setattr('builtins.input', lambda prompt='': seen_prompts.append(prompt) or '')
        console = OperatorConsole()
        console.prompt('banner', action='rotate the dial')
        assert seen_prompts == ['  → rotate the dial (Ctrl-C to abort): ']

    def test_eof_becomes_keyboard_interrupt(self, monkeypatch):
        def raise_eof(prompt=''):
            raise EOFError

        monkeypatch.setattr('builtins.input', raise_eof)
        console = OperatorConsole()
        with pytest.raises(KeyboardInterrupt):
            console.prompt('banner')


class TestConfirmOk:
    def test_accepts_expected_variants(self, monkeypatch):
        console = OperatorConsole()
        for accept in ('', 'y', 'Y', 'yes', 'YES', ' ok ', 'OK', '  y  ', '  '):
            monkeypatch.setattr('builtins.input', lambda _prompt, _a=accept: _a)
            assert console.confirm_ok() is True

    def test_rejects_other_input(self, monkeypatch):
        console = OperatorConsole()
        for reject in ('n', 'no', 'r', 'retry', 'redo', 'x', 'nope'):
            monkeypatch.setattr('builtins.input', lambda _prompt, _r=reject: _r)
            assert console.confirm_ok() is False

    def test_prompt_text_and_default_what(self, monkeypatch):
        seen = []
        monkeypatch.setattr('builtins.input', lambda p: seen.append(p) or '')
        OperatorConsole().confirm_ok()
        assert seen == ['  → OK with this measurement? [Y / r=repeat] ']

    def test_prompt_text_with_custom_what(self, monkeypatch):
        seen = []
        monkeypatch.setattr('builtins.input', lambda p: seen.append(p) or '')
        OperatorConsole().confirm_ok('null capture')
        assert seen == ['  → OK with null capture? [Y / r=repeat] ']

    def test_custom_hint_replaces_the_repeat_wording(self, monkeypatch):
        # A caller whose "anything else" branch aborts rather than repeats
        # must be able to say so: the inline hint is the last thing the
        # operator reads before typing.
        seen = []
        monkeypatch.setattr('builtins.input', lambda p: seen.append(p) or '')
        OperatorConsole().confirm_ok(
            'stepping to the next fault',
            hint='[Enter or y = continue / anything else = abort]')
        assert seen == ['  → OK with stepping to the next fault? '
                        '[Enter or y = continue / anything else = abort] ']

    def test_custom_hint_does_not_change_what_is_accepted(self, monkeypatch):
        console = OperatorConsole()
        monkeypatch.setattr('builtins.input', lambda _p: 'y')
        assert console.confirm_ok('x', hint='[whatever]') is True
        monkeypatch.setattr('builtins.input', lambda _p: 'r')
        assert console.confirm_ok('x', hint='[whatever]') is False

    def test_default_hint_is_unchanged_for_existing_callers(self, monkeypatch):
        seen = []
        monkeypatch.setattr('builtins.input', lambda p: seen.append(p) or '')
        OperatorConsole().confirm_ok('null capture', hint=None)
        assert seen == ['  → OK with null capture? [Y / r=repeat] ']

    def test_eof_becomes_keyboard_interrupt(self, monkeypatch):
        def raise_eof(prompt=''):
            raise EOFError

        monkeypatch.setattr('builtins.input', raise_eof)
        with pytest.raises(KeyboardInterrupt):
            OperatorConsole().confirm_ok()


class TestConfirmOkDefaultNo:
    """M26: a confirmation that guards energizing may not take Enter as yes.

    ``default=False`` is for the prompts where "anything else stops" is the
    documented behaviour and a bare Enter -- the answer an operator gives by
    leaning on the keyboard -- has to mean stop.
    """

    def test_a_bare_enter_is_a_no(self, monkeypatch):
        console = OperatorConsole()
        for empty in ('', '   ', '\t'):
            monkeypatch.setattr('builtins.input', lambda _p, _e=empty: _e)
            assert console.confirm_ok('running with no ambient',
                                      hint='[y = run / anything else = stop]',
                                      default=False) is False

    def test_an_explicit_yes_still_runs(self, monkeypatch):
        console = OperatorConsole()
        for accept in ('y', 'Y', 'yes', 'YES', ' ok '):
            monkeypatch.setattr('builtins.input', lambda _p, _a=accept: _a)
            assert console.confirm_ok('x', default=False) is True

    def test_the_default_is_unchanged_for_every_existing_caller(self,
                                                                monkeypatch):
        monkeypatch.setattr('builtins.input', lambda _p: '')
        assert OperatorConsole().confirm_ok() is True


class TestConfirmExplicit:
    def test_accepts_only_full_yes(self, monkeypatch):
        console = OperatorConsole()
        for accept in ('yes', 'YES', 'Yes', ' yes ', 'yes\n'):
            monkeypatch.setattr('builtins.input', lambda _prompt, _a=accept: _a)
            assert console.confirm_explicit('bad reading') is True

    def test_rejects_bare_y(self, monkeypatch):
        # The whole point of this helper is that a single 'y' must NOT
        # accept a gross-error override.
        console = OperatorConsole()
        monkeypatch.setattr('builtins.input', lambda _prompt: 'y')
        assert console.confirm_explicit('bad reading') is False
        monkeypatch.setattr('builtins.input', lambda _prompt: 'Y')
        assert console.confirm_explicit('bad reading') is False

    def test_rejects_other_input(self, monkeypatch):
        console = OperatorConsole()
        for reject in ('', 'ok', 'OK', 'sure', 'n', 'no', 'retry', 'r'):
            monkeypatch.setattr('builtins.input', lambda _prompt, _r=reject: _r)
            assert console.confirm_explicit('bad reading') is False

    def test_prompt_text(self, monkeypatch):
        seen = []
        monkeypatch.setattr('builtins.input', lambda p: seen.append(p) or '')
        OperatorConsole().confirm_explicit('wrong reading')
        assert seen == [
            '  → Override and accept wrong reading? '
            '[type "yes" to accept / Enter to repeat] '
        ]

    def test_default_hint_is_unchanged_for_existing_callers(self, monkeypatch):
        # kvd_linearity, cal_verify and the 5700A service console all rely on
        # the "Enter to repeat" wording, which is true for them.
        seen = []
        monkeypatch.setattr('builtins.input', lambda p: seen.append(p) or '')
        OperatorConsole().confirm_explicit('wrong reading', hint=None)
        assert seen == [
            '  → Override and accept wrong reading? '
            '[type "yes" to accept / Enter to repeat] '
        ]

    def test_custom_hint_replaces_the_repeat_wording(self, monkeypatch):
        # A caller whose False branch is a permanent skip must be able to say
        # so: promising a second chance that never comes is worse than no
        # hint at all. The calibration-adjustment runner is the worked case.
        seen = []
        monkeypatch.setattr('builtins.input', lambda p: seen.append(p) or '')
        OperatorConsole().confirm_explicit(
            'committing new calibration constants',
            hint='[type "yes" to commit / anything else = SKIP permanently]')
        assert seen == [
            '  → Override and accept committing new calibration constants? '
            '[type "yes" to commit / anything else = SKIP permanently] '
        ]

    def test_custom_hint_does_not_change_what_is_accepted(self, monkeypatch):
        console = OperatorConsole()
        monkeypatch.setattr('builtins.input', lambda _p: 'yes')
        assert console.confirm_explicit('x', hint='[whatever]') is True
        monkeypatch.setattr('builtins.input', lambda _p: 'y')
        assert console.confirm_explicit('x', hint='[whatever]') is False

    def test_eof_becomes_keyboard_interrupt(self, monkeypatch):
        def raise_eof(prompt=''):
            raise EOFError

        monkeypatch.setattr('builtins.input', raise_eof)
        with pytest.raises(KeyboardInterrupt):
            OperatorConsole().confirm_explicit('bad reading')


class TestAsk:
    def test_returns_the_typed_line(self, monkeypatch):
        monkeypatch.setattr('builtins.input', lambda _p: '10.00013')
        assert OperatorConsole().ask('certified value: ') == '10.00013'

    def test_strips_surrounding_whitespace(self, monkeypatch):
        monkeypatch.setattr('builtins.input', lambda _p: '  1.5 ppm  ')
        assert OperatorConsole().ask('uncertainty: ') == '1.5 ppm'

    def test_empty_input_is_an_empty_string_not_none(self, monkeypatch):
        # Optional fields (an artefact serial) rely on this.
        monkeypatch.setattr('builtins.input', lambda _p: '')
        assert OperatorConsole().ask('serial (optional): ') == ''

    def test_question_is_rendered_verbatim_after_the_marker(self, monkeypatch):
        seen = []
        monkeypatch.setattr('builtins.input', lambda p: seen.append(p) or '')
        OperatorConsole().ask("uncertainty, WITH a unit [e.g. '1.5 ppm']: ")
        assert seen == [
            "  → uncertainty, WITH a unit [e.g. '1.5 ppm']: "]

    def test_panel_push_is_optional_and_truncated(self, monkeypatch):
        calls = []
        monkeypatch.setattr('builtins.input', lambda _p: 'x')
        console = OperatorConsole(panel=calls.append)
        console.ask('value: ', panel_msg='this is far longer than sixteen')
        assert calls == ['this is far long']
        assert len(calls[0]) == PANEL_W

    def test_panel_exception_does_not_lose_the_answer(self, monkeypatch):
        def boom(msg):
            raise RuntimeError('front panel is dead')

        monkeypatch.setattr('builtins.input', lambda _p: 'kept')
        console = OperatorConsole(panel=boom)
        assert console.ask('value: ', panel_msg='hi') == 'kept'

    def test_no_panel_push_without_a_message(self, monkeypatch):
        calls = []
        monkeypatch.setattr('builtins.input', lambda _p: 'x')
        OperatorConsole(panel=calls.append).ask('value: ')
        assert calls == []

    def test_eof_becomes_keyboard_interrupt(self, monkeypatch):
        # Same convention as prompt/confirm_ok/confirm_explicit: a closed
        # stdin is an abandoned session, and every caller handles Ctrl-C.
        def raise_eof(prompt=''):
            raise EOFError

        monkeypatch.setattr('builtins.input', raise_eof)
        with pytest.raises(KeyboardInterrupt):
            OperatorConsole().ask('value: ')


class TestForInstrument:
    def test_wires_panel_from_display_message(self):
        calls = []

        class Dev:
            def display_message(self, msg):
                calls.append(msg)

        console = OperatorConsole.for_instrument(Dev())
        console.show('banner', panel_msg='hello')
        assert calls == ['hello']

    def test_none_for_device_without_display_message(self):
        class Dev:
            pass

        console = OperatorConsole.for_instrument(Dev())
        assert console._panel is None
        # Must still work fine without raising.
        console.show('banner')

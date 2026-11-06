"""Operator-console helpers: banners, confirmations, and an optional push of
short status strings to an instrument's front panel.

Extracted from ``apps/kvd_linearity.py`` so the interactive apps (KVD
linearity, Fluke 5700A service/manual console, calibration verification,
self-cal) share one implementation instead of growing separate copies.
"""

from typing import Callable, Optional

# The HP 3458A front panel can display exactly 16 characters. Every panel
# string is truncated to this width as a defensive safety net; the call sites
# are also crafted to fit naturally so the operator sees a useful message.
PANEL_W = 16


class OperatorConsole:
    """Bundles the banner/prompt/confirm helpers used to drive an operator
    through a manual measurement procedure, plus an optional best-effort
    push of a short status string to the instrument's front panel.

    ``panel`` is a callable taking the already-truncated panel string (e.g.
    an instrument's ``display_message`` bound method); pass ``None`` if the
    instrument has no front panel or the app doesn't want to use it. Use
    ``OperatorConsole.for_instrument(dev)`` to wire it up automatically."""

    def __init__(self, *, panel: Optional[Callable[[str], None]] = None) -> None:
        self._panel = panel

    @classmethod
    def for_instrument(cls, dev) -> 'OperatorConsole':
        """Convenience constructor: wires ``panel`` from ``dev``'s
        ``display_message`` method if it has one, else leaves it ``None``."""
        return cls(panel=getattr(dev, 'display_message', None))

    def show(self, banner: str, *body: str,
              panel_msg: Optional[str] = None) -> None:
        """Print a banner + arbitrary body lines, and optionally push a short
        summary to the instrument's front panel.

        ``panel_msg`` is the front-panel string (truncated to PANEL_W chars).
        If omitted we use the banner — typically that's wider than the panel,
        so callers should pass a hand-tuned 16-char string when the meaning
        matters."""
        print()
        print(banner, flush=True)
        for line in body:
            # Preserve intentional blank-line separators in the body.
            print(f'  {line}' if line else '', flush=True)
        if self._panel is not None:
            msg = (panel_msg if panel_msg is not None
                   else banner.strip(' ─'))
            try:
                self._panel(msg[:PANEL_W])
            except Exception:
                pass

    def prompt(self, banner: str, *body: str,
               panel_msg: Optional[str] = None,
               action: str = 'press Enter when the action above is complete',
               ) -> None:
        """Show banner + body, then block on Enter. ``action`` is the
        call-to-action rendered at the input() line so the operator always
        knows what to do."""
        self.show(banner, *body, panel_msg=panel_msg)
        try:
            input(f'  → {action} (Ctrl-C to abort): ')
        except EOFError:
            raise KeyboardInterrupt

    #: Inline hint appended to :meth:`confirm_ok`'s prompt. Describes the
    #: measurement-repeat use it was extracted from; a caller whose "anything
    #: else" branch does something else must pass its own ``hint``.
    CONFIRM_OK_HINT = '[Y / r=repeat]'

    def confirm_ok(self, what: str = 'this measurement',
                   *, hint: Optional[str] = None,
                   default: bool = True) -> bool:
        """Ask the operator whether the just-completed capture is acceptable.
        Enter, ``y`` or ``yes`` returns True (accept and move on); anything
        else returns False so the caller can re-run the current measurement
        set without restarting the whole run.

        ``hint`` overrides the inline ``[Y / r=repeat]`` shown at the input
        line. **Pass one whenever "anything else" does not mean "repeat"** —
        the hint is the last thing the operator reads before typing, so it
        must describe what this particular call actually does. The 5700A
        console's diagnostic-stepping prompt is the worked example: there,
        anything other than Enter/``y`` sends ``DIAG ABORT``, and the default
        hint would be inviting the operator to type the very key that stops
        the diagnostic.

        ``default=False`` makes a **bare Enter a "no"**. Pass it on every
        prompt that guards energizing an output or starting a run that cannot
        produce a verdict: Enter is the answer an operator gives without
        reading, and a prompt whose hint says "anything else stops" must not
        then treat the emptiest possible answer as "run anyway". The
        verification runner's no-ambient confirmation is the worked example —
        it can forfeit two hours of measurements to a keystroke."""
        marker = self.CONFIRM_OK_HINT if hint is None else hint
        try:
            ans = input(f'  → OK with {what}? {marker} ').strip().lower()
        except EOFError:
            raise KeyboardInterrupt
        if not ans:
            return bool(default)
        return ans in ('y', 'yes', 'ok')

    #: Inline hint appended to :meth:`confirm_explicit`'s prompt. Describes
    #: the gross-error-override use it was extracted from, where declining
    #: means "repeat the step". A caller whose False branch does something
    #: else — skips permanently, aborts — **must** pass its own ``hint``.
    CONFIRM_EXPLICIT_HINT = '[type "yes" to accept / Enter to repeat]'

    def confirm_explicit(self, what: str,
                         *, hint: Optional[str] = None) -> bool:
        """Gross-error override prompt — requires the operator to type the
        full word ``yes`` (case-insensitive) to accept an out-of-bounds
        value. Enter, ``y``, or anything else returns False so the caller
        retries the step.

        Used only when a sanity check has FAILED — a deliberate friction
        step against fat-finger acceptance of a clearly-wrong reading (wrong
        dial, wrong source, leads in the wrong jack, etc.).

        ``hint`` overrides the inline ``[type "yes" to accept / Enter to
        repeat]``, for the same reason :meth:`confirm_ok` has one: the hint is
        the last thing the operator reads before typing, so it must describe
        what *this* call does with a False. The calibration-adjustment runner
        is the worked example — there a False is a permanent skip of an
        irreversible step, and the default hint would promise a second chance
        that never comes."""
        marker = self.CONFIRM_EXPLICIT_HINT if hint is None else hint
        try:
            ans = input(f'  → Override and accept {what}? '
                        f'{marker} ').strip().lower()
        except EOFError:
            raise KeyboardInterrupt
        return ans == 'yes'

    def ask(self, question: str, *,
            panel_msg: Optional[str] = None) -> str:
        """Free-text entry — one line, stripped, returned verbatim.

        The other three helpers all answer a *yes/no*; this is the one that
        collects a value the operator has to read off something (a
        certificate's certified value and its uncertainty, an ambient
        temperature, an artefact's serial). Validation belongs to the caller,
        which is the only code that knows what a plausible answer looks like;
        this method only gets the characters.

        ``question`` is rendered verbatim after the ``→`` marker, so it should
        carry its own trailing punctuation and any units or example, e.g.
        ``"expanded uncertainty (k=2), WITH a unit [e.g. '1.5 ppm']: "``.

        ``EOFError`` becomes :class:`KeyboardInterrupt`, matching
        :meth:`prompt`, :meth:`confirm_ok` and :meth:`confirm_explicit`: a
        closed stdin is an abandoned session, and every caller already has to
        handle Ctrl-C."""
        if panel_msg is not None and self._panel is not None:
            try:
                self._panel(panel_msg[:PANEL_W])
            except Exception:
                pass
        try:
            return input(f'  → {question}').strip()
        except EOFError:
            raise KeyboardInterrupt

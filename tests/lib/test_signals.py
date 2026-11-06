"""Tests for caldmm.lib.signals."""

import signal

from caldmm.lib.signals import setup_graceful_shutdown


class TestSetupGracefulShutdown:
    def test_installs_sigint_handler(self):
        old_handler = signal.getsignal(signal.SIGINT)
        setup_graceful_shutdown()
        handler = signal.getsignal(signal.SIGINT)
        assert handler is not signal.SIG_DFL
        assert handler is not old_handler
        # Restore
        signal.signal(signal.SIGINT, old_handler)

    def test_installs_with_cleanup(self):
        old_handler = signal.getsignal(signal.SIGINT)
        called = []
        setup_graceful_shutdown(cleanup=lambda: called.append(True))
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        # Restore
        signal.signal(signal.SIGINT, old_handler)

    def test_none_cleanup_accepted(self):
        old_handler = signal.getsignal(signal.SIGINT)
        setup_graceful_shutdown(cleanup=None)
        handler = signal.getsignal(signal.SIGINT)
        assert callable(handler)
        signal.signal(signal.SIGINT, old_handler)

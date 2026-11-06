"""Graceful shutdown signal handling.

One SIGINT/SIGTERM handler shared by every long-running tool, so a Ctrl-C
always runs the tool's cleanup (standby, close the CSV) before exiting.
"""

import signal
import sys
from typing import Callable, Optional


def setup_graceful_shutdown(cleanup: Optional[Callable[[], None]] = None) -> None:
    """Install a SIGINT handler that runs *cleanup* (if provided), then exits.

    On the first Ctrl-C the handler runs cleanup and exits with code 1.
    If cleanup itself is interrupted, the default handler takes over so a
    second Ctrl-C always terminates immediately.
    """

    def _handler(signum, frame):
        print("\nCaught SIGINT, stopping...")
        # Restore default so a second Ctrl-C kills immediately
        signal.signal(signal.SIGINT, signal.SIG_DFL)
        if cleanup is not None:
            cleanup()
        sys.exit(1)

    signal.signal(signal.SIGINT, _handler)

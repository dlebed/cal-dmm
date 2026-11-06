"""Cross-platform GPIB device factory: linux-gpib on Linux, VISA elsewhere.

Every GPIB driver in this package opens its instrument with::

    from caldmm.drivers import gpib as Gpib
    dev = Gpib.Gpib(bus, pad, timeout=tcode)

which is the constructor signature of linux-gpib's ``Gpib.Gpib``.  This module
keeps that signature and that device surface (``write``, ``read``,
``serial_poll``, ``clear``, ``timeout``, ``ibloc``, ``close``) and picks the
backend underneath:

``linux-gpib``
    The ``Gpib`` Python binding of linux-gpib.  The returned object *is* the
    native ``Gpib.Gpib``, so behaviour on Linux is unchanged.

``visa``
    Any VISA implementation reachable through ``pyvisa``: NI-VISA with
    NI-488.2, or Keysight IO Libraries Suite, on **Windows** (and macOS / Linux
    if installed there).  The instrument is opened as ``GPIB<bus>::<pad>::INSTR``
    and wrapped in :class:`VisaGpibDevice`, which speaks the linux-gpib surface.

Selection, in order:

1. ``CALDMM_GPIB_BACKEND`` = ``linux-gpib`` or ``visa``, if set;
2. ``visa`` on Windows (``sys.platform == "win32"``);
3. ``linux-gpib`` when the ``Gpib`` module imports;
4. ``visa`` otherwise.

``CALDMM_VISA_LIBRARY`` is passed to ``pyvisa.ResourceManager`` — leave it
unset for the system VISA (NI or Keysight), or set e.g. ``@py`` for pyvisa-py.

Timeouts are linux-gpib **T-codes** on this surface (``11`` = 1 s, ``13`` =
10 s, ``17`` = 1000 s); :class:`VisaGpibDevice` converts them to milliseconds.
"""

from __future__ import annotations

import os
import sys
from typing import Optional

#: linux-gpib T-code -> seconds (``ibtmo``).  0 disables the timeout.
TCODE_SECONDS: dict[int, Optional[float]] = {
    0: None,
    1: 10e-6, 2: 30e-6, 3: 100e-6, 4: 300e-6,
    5: 1e-3, 6: 3e-3, 7: 10e-3, 8: 30e-3, 9: 100e-3, 10: 300e-3,
    11: 1.0, 12: 3.0, 13: 10.0, 14: 30.0, 15: 100.0, 16: 300.0, 17: 1000.0,
}

BACKENDS = ("linux-gpib", "visa")

#: VISA ``viGpibControlREN`` mode: send GTL (go to local) to the device.
_VI_GPIB_REN_ADDRESS_GTL = 6


class GpibBackendError(RuntimeError):
    """No usable GPIB backend, or an unknown one was requested."""


def tcode_to_seconds(tcode: int) -> Optional[float]:
    """Seconds for a linux-gpib T-code (``None`` = no timeout)."""
    try:
        return TCODE_SECONDS[int(tcode)]
    except (KeyError, ValueError, TypeError):
        raise ValueError(f"not a GPIB T-code (0..17): {tcode!r}") from None


def visa_resource_name(bus: int, pad: int) -> str:
    """``GPIB<bus>::<pad>::INSTR`` — the VISA name of a bus/primary address."""
    return f"GPIB{int(bus)}::{int(pad)}::INSTR"


def _linux_gpib_available() -> bool:
    try:
        import Gpib as _native  # noqa: F401
    except ImportError:
        return False
    return True


def select_backend() -> str:
    """The backend :func:`Gpib` will use (see the module docstring)."""
    wanted = os.environ.get("CALDMM_GPIB_BACKEND", "").strip().lower()
    if wanted:
        if wanted not in BACKENDS:
            raise GpibBackendError(
                f"CALDMM_GPIB_BACKEND={wanted!r}; expected one of "
                f"{', '.join(BACKENDS)}")
        return wanted
    if sys.platform == "win32":
        return "visa"
    if _linux_gpib_available():
        return "linux-gpib"
    return "visa"


class VisaGpibDevice:
    """A pyvisa GPIB resource behind linux-gpib's ``Gpib.Gpib`` surface."""

    def __init__(self, bus: int, pad: int, *, timeout: int = 13,
                 resource_manager=None):
        if resource_manager is None:
            try:
                import pyvisa
            except ImportError as exc:
                raise GpibBackendError(
                    "the VISA GPIB backend needs pyvisa "
                    "(pip install 'caldmm[visa]') and a VISA library "
                    "(NI-VISA + NI-488.2, or Keysight IO Libraries)") from exc
            library = os.environ.get("CALDMM_VISA_LIBRARY", "")
            resource_manager = pyvisa.ResourceManager(library)
        self.resource_name = visa_resource_name(bus, pad)
        self._rm = resource_manager
        self._res = resource_manager.open_resource(self.resource_name)
        # linux-gpib reads until EOI with no termination character; do the
        # same so a block response arrives exactly as the instrument sent it.
        self._res.read_termination = None
        self._res.write_termination = "\n"
        self.timeout(timeout)

    #: linux-gpib exposes the board descriptor as ``id``; VISA has none.
    id = None

    def write(self, cmd) -> None:
        if isinstance(cmd, (bytes, bytearray)):
            self._res.write_raw(bytes(cmd))
        else:
            self._res.write(str(cmd))

    def read(self, length: int = 512) -> bytes:
        """Bytes up to EOI, as linux-gpib returns them."""
        return bytes(self._res.read_raw())

    def serial_poll(self) -> int:
        return int(self._res.read_stb())

    def clear(self) -> None:
        self._res.clear()

    def timeout(self, tcode: int) -> None:
        seconds = tcode_to_seconds(tcode)
        self._res.timeout = None if seconds is None else seconds * 1000.0

    def ibloc(self) -> None:
        """Go to local (GTL), linux-gpib's ``ibloc``."""
        self._res.control_ren(_VI_GPIB_REN_ADDRESS_GTL)

    def close(self) -> None:
        self._res.close()


def Gpib(bus: int, pad: int, timeout: int = 13, *,
         backend: Optional[str] = None):
    """Open GPIB device *pad* on board *bus*; linux-gpib's signature.

    *timeout* is a linux-gpib T-code.  *backend* overrides
    :func:`select_backend`.
    """
    chosen = backend or select_backend()
    if chosen == "linux-gpib":
        try:
            import Gpib as _native
        except ImportError as exc:
            raise GpibBackendError(
                "the linux-gpib backend needs the linux-gpib Python "
                "binding (module 'Gpib')") from exc
        return _native.Gpib(bus, pad, timeout=timeout)
    if chosen == "visa":
        return VisaGpibDevice(bus, pad, timeout=timeout)
    raise GpibBackendError(f"unknown GPIB backend {chosen!r}")

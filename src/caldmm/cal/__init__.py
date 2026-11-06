"""Calibration and verification subsystem.

Bidirectional by design: a calibrator can be used as the reference to verify a
DMM, and a golden DMM can be used as the reference to verify a calibrator.  The
two directions share one specification model, so the same YAML that says what a
34401A is good for also says what has to be true for it to serve as a reference.

**Architectural invariant:** this package must stay importable with no hardware
libraries present.  ``quantities`` imports nothing from the project;
``specs`` imports only ``quantities``, the stdlib and ``yaml``.

:mod:`caldmm.drivers` may appear in exactly two modules, and in both only
**inside a function**, so the import happens when a real instrument is already
on the bench:

* :mod:`adapters`, which is the layer whose whole job is to construct drivers;
* :mod:`dmm_procedures`, for the ``ACAL`` duration and the ``ERR?``/``AUXERR?``
  bit decoders of :mod:`caldmm.drivers.hp3458a`, in the one step that sends
  ``ACAL AC`` with ``cmd()`` rather than through the driver.  Routing those
  through ``adapters`` would put a 3458A-specific error table in the generic
  adapter layer and spread the private-symbol coupling across two modules
  instead of one; the guard values this module *must* have before any I/O are
  mirrored as constants instead, precisely so they need no import at all.

Both are pinned by ``sys.meta_path`` blocker tests, one per module in ``cal``.
"""

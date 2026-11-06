"""Instrument drivers for caldmm.

Driver imports are lazy to avoid requiring hardware-specific libraries
(linux-gpib, pyserial) at import time. Import drivers
directly when needed:

    from caldmm.drivers.hp3458a import HP3458A
"""

from caldmm.drivers._base import MultimeterBase, WorkerBase

__all__ = [
    "MultimeterBase", "WorkerBase",
]


def __getattr__(name):
    """Lazy-load driver classes on first access."""
    _lazy = {
        'HP3458A': ('caldmm.drivers.hp3458a', 'HP3458A'),
        'HP3458A_Func': ('caldmm.drivers.hp3458a', 'Func'),
        'ACAL': ('caldmm.drivers.hp3458a', 'ACAL'),
        'CalItem': ('caldmm.drivers.hp3458a', 'CalItem'),
        'HP3458A_Worker': ('caldmm.drivers.hp3458a', 'Worker'),
        'Fluke5700A': ('caldmm.drivers.fluke5700a', 'Fluke5700A'),
    }
    if name in _lazy:
        module_path, attr = _lazy[name]
        import importlib
        mod = importlib.import_module(module_path)
        return getattr(mod, attr)
    raise AttributeError(f"module 'caldmm.drivers' has no attribute {name!r}")

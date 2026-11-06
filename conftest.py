"""
Root conftest.py: install minimal stubs for hardware libraries so that
driver modules can be imported in the test environment without real hardware
packages installed.
"""
import sys
from unittest.mock import MagicMock

for mod in ("serial", "Gpib", "pyvisa", "smbus"):
    if mod not in sys.modules:
        sys.modules[mod] = MagicMock()

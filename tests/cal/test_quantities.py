"""Tests for the shared quantity / connection vocabulary."""

import subprocess
import sys
import textwrap

import pytest

from caldmm.cal.quantities import Connection, Quantity


class TestQuantity:
    def test_members(self):
        assert {q.name for q in Quantity} == {
            "DCV", "ACV", "DCI", "ACI", "RES", "WBV"}

    @pytest.mark.parametrize("quantity,unit", [
        (Quantity.DCV, "V"),
        (Quantity.ACV, "V"),
        (Quantity.DCI, "A"),
        (Quantity.ACI, "A"),
        (Quantity.RES, "OHM"),
        (Quantity.WBV, "V"),
    ])
    def test_unit(self, quantity, unit):
        assert quantity.unit == unit

    @pytest.mark.parametrize("quantity,expected", [
        (Quantity.DCV, False),
        (Quantity.DCI, False),
        (Quantity.RES, False),
        (Quantity.ACV, True),
        (Quantity.ACI, True),
        (Quantity.WBV, True),
    ])
    def test_frequency_dependent(self, quantity, expected):
        assert quantity.frequency_dependent is expected

    def test_from_value_round_trip(self):
        for quantity in Quantity:
            assert Quantity(quantity.value) is quantity

    def test_case_insensitive(self):
        assert Quantity("dcv") is Quantity.DCV
        assert Quantity(" AcV ") is Quantity.ACV

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            Quantity("VOLT:DC")


class TestConnection:
    def test_yaml_spellings(self):
        assert Connection.TWO_WIRE.value == "2w"
        assert Connection.FOUR_WIRE.value == "4w"
        assert Connection.TWO_WIRE_RCOMP.value == "2w_rcomp"

    def test_round_trip(self):
        for connection in Connection:
            assert Connection(connection.value) is connection

    def test_case_insensitive_and_by_name(self):
        assert Connection("4W") is Connection.FOUR_WIRE
        assert Connection("four_wire") is Connection.FOUR_WIRE

    def test_unknown_raises(self):
        with pytest.raises(ValueError):
            Connection("3w")


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

    from caldmm.cal.quantities import Connection, Quantity

    # Usable, not merely importable.
    assert Quantity("dcv") is Quantity.DCV
    assert Quantity.RES.unit == "OHM"
    assert Connection("4W") is Connection.FOUR_WIRE

    # This is the bottom of the package: it imports nothing from the project
    # at all, which is what lets every other cal module depend on it freely.
    pulled = sorted(m for m in sys.modules if m.startswith("caldmm.")
                    and m != "caldmm.cal.quantities")
    assert pulled == ["caldmm.cal"], pulled
    print("OK")
    """)


def test_quantities_imports_with_no_hardware_libraries():
    """The bottom of the dependency order, pinned.

    ``cal/__init__`` states that ``quantities`` imports nothing from the
    project; every other module in the package leans on that.  A subprocess
    with a real ``sys.meta_path`` blocker, because the root conftest's mock
    stubs would otherwise let a driver import succeed and prove nothing.
    """
    completed = subprocess.run([sys.executable, "-c", IMPORT_HYGIENE_SCRIPT],
                               capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert "OK" in completed.stdout

"""Canonical measurands and connection topologies.

The shared vocabulary between sources and meters, so that nothing downstream
has to care that the 3458A calls dc volts ``DCV`` while a SCPI meter calls it
``VOLT:DC``.  The instrument-facing spelling lives in the per-model YAML
(``func:``) and is resolved by the adapter layer -- deliberately not here.

This module imports nothing from the project and nothing outside the stdlib:
:mod:`caldmm.cal` must stay importable with no hardware libraries present.
"""

from enum import Enum


class Quantity(Enum):
    """A measurand, independent of any instrument's own function names."""

    DCV = "DCV"          # dc volts
    ACV = "ACV"          # ac volts, normal (true-rms or thermal) path
    DCI = "DCI"          # dc current
    ACI = "ACI"          # ac current
    RES = "RES"          # resistance
    WBV = "WBV"          # wideband ac volts (5700A option 03, 3458A ACV SETACV)

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            return cls.__members__.get(value.strip().upper())
        return None

    @property
    def unit(self) -> str:
        """Canonical SI unit symbol for this quantity."""
        return _UNITS[self]

    @property
    def frequency_dependent(self) -> bool:
        """True when the specification depends on frequency (the ac quantities)."""
        return self in _FREQUENCY_DEPENDENT


class Connection(Enum):
    """How the DUT is wired.  Values are the YAML spellings."""

    TWO_WIRE = "2w"
    FOUR_WIRE = "4w"
    TWO_WIRE_RCOMP = "2w_rcomp"   # 2-wire with lead resistance nulled out

    @classmethod
    def _missing_(cls, value):
        if isinstance(value, str):
            wanted = value.strip().lower()
            for member in cls:
                if member.value == wanted or member.name.lower() == wanted:
                    return member
        return None


class Terminals(Enum):
    """Which physical input terminals of the meter a quantity is wired to.

    This, not the quantity, is what decides whether the operator has to touch
    a lead.  One 4-wire cable set (calibrator OUTPUT and SENSE to the meter's
    HI/LO and ohms-sense inputs, plus guard) serves dc volts, ac volts and both
    resistance wirings; only the current functions need the HI lead moved to
    the meter's current input, and the 5700A's wideband option has its own
    Type-N output.  A plan's wiring groups are runs of points sharing one of
    these, so a full verification of the 3458A re-cables exactly once.
    """

    VOLTAGE = "voltage"     # V/ohm input: DCV, ACV, RES (2w and 4w)
    CURRENT = "current"     # the meter's current input: DCI, ACI
    WIDEBAND = "wideband"   # 5700A option 03 Type-N output: WBV

    @classmethod
    def of(cls, quantity) -> "Terminals":
        """The terminal set *quantity* is measured on."""
        return _TERMINALS[Quantity(quantity)]


_TERMINALS = {
    Quantity.DCV: Terminals.VOLTAGE,
    Quantity.ACV: Terminals.VOLTAGE,
    Quantity.RES: Terminals.VOLTAGE,
    Quantity.DCI: Terminals.CURRENT,
    Quantity.ACI: Terminals.CURRENT,
    Quantity.WBV: Terminals.WIDEBAND,
}

_UNITS = {
    Quantity.DCV: "V",
    Quantity.ACV: "V",
    Quantity.DCI: "A",
    Quantity.ACI: "A",
    Quantity.RES: "OHM",
    Quantity.WBV: "V",
}

_FREQUENCY_DEPENDENT = frozenset({Quantity.ACV, Quantity.ACI, Quantity.WBV})

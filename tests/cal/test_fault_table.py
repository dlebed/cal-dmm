"""The offline 5700A fault table must equal the driver's, exactly.

``caldmm.cal`` may not import a driver at run time, so the 692 fault messages
have to exist in two places: the driver's ``FAULT_CODES`` (the transcription of
record: guide section 12.1 merged over the Service Manual chapter-4 table) and
``cal/data/fluke5700a_faults.yaml`` (the copy ``cal`` can read).  Two copies
drift unless something forces them not to.  This module is that something.

The driver import here is deliberate and allowed: the root ``conftest.py``
stubs ``Gpib``, and it is *tests* that may import a driver, never ``cal``
itself.
"""

from importlib import resources

import pytest
import yaml

from caldmm.cal.specs import SpecError, available_models, load_model
from caldmm.drivers.fluke5700a import FAULT_CODES

FAULT_FILE = "fluke5700a_faults.yaml"


@pytest.fixture(scope="module")
def shipped_faults():
    text = (resources.files("caldmm.cal.data") / FAULT_FILE).read_text("utf-8")
    return yaml.safe_load(text)


class TestFaultTable:
    def test_shipped_but_not_advertised_as_a_model(self):
        # It has to sit in the data package (cal must reach it without
        # importing a driver) but it is not a model, so available_models()
        # must not offer it: the obvious `for m in available_models():
        # load_model(m)` loop would crash on it.
        assert (resources.files("caldmm.cal.data") / FAULT_FILE).is_file()
        assert FAULT_FILE[:-5] not in available_models()

    def test_exactly_equal_to_the_driver_dict(self, shipped_faults):
        # Not "is a superset of", not "agrees where both have a key".  Equal.
        assert shipped_faults["faults"] == FAULT_CODES

    def test_same_key_set(self, shipped_faults):
        assert set(shipped_faults["faults"]) == set(FAULT_CODES)

    def test_the_documented_size(self, shipped_faults):
        # The guide's 191 reviewed entries plus the Service Manual's
        # chapter-4 table: the complete 692-code set.
        assert len(shipped_faults["faults"]) == 692

    def test_keys_are_integers_not_strings(self, shipped_faults):
        # A YAML round trip that quoted the codes would still compare equal to
        # nothing and silently decode every fault as "unknown".
        assert all(isinstance(key, int) for key in shipped_faults["faults"])

    def test_a_message_survives_its_colon(self, shipped_faults):
        # "A15: HV +DC High Voltage Output Fault" is the shape that a naive
        # hand-written YAML mapping truncates at the colon.
        assert shipped_faults["faults"][3111] == FAULT_CODES[3111]
        assert ":" in shipped_faults["faults"][3111]

    def test_no_stray_top_level_keys(self, shipped_faults):
        assert set(shipped_faults) == {"faults"}

    def test_is_not_loadable_as_a_model(self):
        # And the refusal says "unknown model id", not a confusing complaint
        # about an unknown top-level key.
        with pytest.raises(SpecError, match="unknown model id"):
            load_model("fluke5700a_faults")

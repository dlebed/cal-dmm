"""``caldmm.drivers.gpib``: backend selection and the VISA shim."""

from unittest.mock import MagicMock

import pytest

from caldmm.drivers import gpib


class TestSelectBackend:
    def test_env_var_wins(self, monkeypatch):
        monkeypatch.setenv("CALDMM_GPIB_BACKEND", "visa")
        assert gpib.select_backend() == "visa"
        monkeypatch.setenv("CALDMM_GPIB_BACKEND", "LINUX-GPIB")
        assert gpib.select_backend() == "linux-gpib"

    def test_unknown_env_value_is_refused(self, monkeypatch):
        monkeypatch.setenv("CALDMM_GPIB_BACKEND", "ni")
        with pytest.raises(gpib.GpibBackendError):
            gpib.select_backend()

    def test_windows_defaults_to_visa(self, monkeypatch):
        monkeypatch.delenv("CALDMM_GPIB_BACKEND", raising=False)
        monkeypatch.setattr(gpib.sys, "platform", "win32")
        monkeypatch.setattr(gpib, "_linux_gpib_available", lambda: True)
        assert gpib.select_backend() == "visa"

    def test_linux_gpib_when_it_imports(self, monkeypatch):
        monkeypatch.delenv("CALDMM_GPIB_BACKEND", raising=False)
        monkeypatch.setattr(gpib.sys, "platform", "linux")
        monkeypatch.setattr(gpib, "_linux_gpib_available", lambda: True)
        assert gpib.select_backend() == "linux-gpib"

    def test_visa_when_linux_gpib_is_absent(self, monkeypatch):
        monkeypatch.delenv("CALDMM_GPIB_BACKEND", raising=False)
        monkeypatch.setattr(gpib.sys, "platform", "linux")
        monkeypatch.setattr(gpib, "_linux_gpib_available", lambda: False)
        assert gpib.select_backend() == "visa"


class TestTcodes:
    @pytest.mark.parametrize("code,seconds", [(11, 1.0), (13, 10.0),
                                              (15, 100.0), (17, 1000.0)])
    def test_known_codes(self, code, seconds):
        assert gpib.tcode_to_seconds(code) == seconds

    def test_zero_disables(self):
        assert gpib.tcode_to_seconds(0) is None

    def test_out_of_range(self):
        with pytest.raises(ValueError):
            gpib.tcode_to_seconds(18)


class TestVisaShim:
    @staticmethod
    def _device(timeout=13):
        rm = MagicMock()
        dev = gpib.VisaGpibDevice(1, 22, timeout=timeout, resource_manager=rm)
        return dev, rm, rm.open_resource.return_value

    def test_resource_name_and_termination(self):
        dev, rm, res = self._device()
        rm.open_resource.assert_called_once_with("GPIB1::22::INSTR")
        assert res.read_termination is None
        assert res.timeout == pytest.approx(10_000.0)

    def test_linux_gpib_surface(self):
        dev, _, res = self._device()
        res.read_raw.return_value = b"+1.0E+01\r\n"
        res.read_stb.return_value = 64
        dev.write("DCV 10")
        res.write.assert_called_once_with("DCV 10")
        assert dev.read() == b"+1.0E+01\r\n"
        assert dev.serial_poll() == 64
        dev.timeout(15)
        assert res.timeout == pytest.approx(100_000.0)
        dev.timeout(0)
        assert res.timeout is None
        dev.clear()
        res.clear.assert_called_once_with()
        dev.ibloc()
        res.control_ren.assert_called_once_with(6)
        dev.close()
        res.close.assert_called_once_with()

    def test_factory_dispatches_to_visa(self, monkeypatch):
        made = []
        monkeypatch.setattr(gpib, "VisaGpibDevice",
                            lambda bus, pad, timeout: made.append(
                                (bus, pad, timeout)) or "dev")
        assert gpib.Gpib(0, 4, timeout=12, backend="visa") == "dev"
        assert made == [(0, 4, 12)]

"""Tests for caldmm.lib.formatting."""

import numpy as np
import pytest

from caldmm.lib.formatting import si_format, ppm, ppm_array


class TestSiFormat:
    def test_millivolt(self):
        result = si_format(0.001, precision=3, unit='V')
        assert 'm' in result
        assert 'V' in result

    def test_zero(self):
        result = si_format(0.0, precision=3, unit='V')
        assert 'V' in result

    def test_large_value(self):
        result = si_format(1234.0, precision=3, unit='V')
        assert 'k' in result

    def test_no_unit(self):
        result = si_format(1.0, precision=3)
        assert result.strip()  # non-empty


class TestPpm:
    def test_zero_deviation(self):
        assert ppm(10.0, 10.0) == pytest.approx(0.0)

    def test_positive_ppm(self):
        # 10.00001 vs 10 = 1 ppm
        assert ppm(10.00001, 10.0) == pytest.approx(1.0, rel=1e-3)

    def test_negative_ppm(self):
        assert ppm(9.99999, 10.0) == pytest.approx(-1.0, rel=1e-3)

    def test_zero_reference(self):
        assert ppm(1.0, 0.0) == 0.0


class TestPpmArray:
    def test_array_output(self):
        values = [10.0, 10.00001, 9.99999]
        result = ppm_array(values, 10.0)
        assert len(result) == 3
        assert result[0] == pytest.approx(0.0)
        assert result[1] == pytest.approx(1.0, rel=1e-3)
        assert result[2] == pytest.approx(-1.0, rel=1e-3)

    def test_numpy_input(self):
        values = np.array([10.0, 10.001])
        result = ppm_array(values, 10.0)
        assert result[1] == pytest.approx(100.0)

    def test_zero_reference_returns_zeros(self):
        result = ppm_array([1.0, 2.0], 0.0)
        assert all(v == 0.0 for v in result)

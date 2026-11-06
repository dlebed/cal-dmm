"""Tests for caldmm.lib.stats."""

import math

import numpy as np
import pytest

from caldmm.lib.stats import compute_stats, moving_average, res_to_pt1000, StatResult


class TestComputeStats:
    def test_returns_stat_result(self):
        result = compute_stats([1.0, 2.0, 3.0])
        assert isinstance(result, StatResult)

    def test_mean(self):
        result = compute_stats([1.0, 2.0, 3.0])
        assert result.mean == pytest.approx(2.0)

    def test_median(self):
        result = compute_stats([1.0, 2.0, 3.0, 100.0])
        assert result.median == pytest.approx(2.5)

    def test_stdev(self):
        result = compute_stats([10.0, 10.0, 10.0])
        assert result.stdev == pytest.approx(0.0)

    def test_stdev_nonzero(self):
        result = compute_stats([1.0, 3.0])
        assert result.stdev == pytest.approx(1.0)

    def test_min_max_pp(self):
        result = compute_stats([5.0, 10.0, 15.0])
        assert result.min == pytest.approx(5.0)
        assert result.max == pytest.approx(15.0)
        assert result.pp == pytest.approx(10.0)

    def test_ppm_values(self):
        samples = [10.0, 10.001, 9.999]
        result = compute_stats(samples)
        assert result.stdev_ppm > 0
        assert result.pp_ppm > 0

    def test_count(self):
        result = compute_stats([1, 2, 3, 4, 5])
        assert result.count == 5

    def test_sigma3_bounds(self):
        result = compute_stats([10.0, 10.0, 10.0])
        assert result.sigma3_plus == pytest.approx(10.0)
        assert result.sigma3_minus == pytest.approx(10.0)

    def test_zero_mean_no_divide_error(self):
        result = compute_stats([-1.0, 0.0, 1.0])
        assert result.stdev_ppm == 0.0
        assert result.pp_ppm == 0.0

    def test_accepts_numpy_array(self):
        result = compute_stats(np.array([1.0, 2.0, 3.0]))
        assert result.mean == pytest.approx(2.0)

    def test_single_sample(self):
        result = compute_stats([42.0])
        assert result.mean == pytest.approx(42.0)
        assert result.stdev == pytest.approx(0.0)
        assert result.pp == pytest.approx(0.0)


class TestPrintStats:
    def test_prints_output(self, capsys):
        from caldmm.lib.stats import print_stats
        result = compute_stats([10.0, 10.001, 9.999])
        print_stats(result, unit='V')
        output = capsys.readouterr().out
        assert 'Mean' in output
        assert 'Median' in output
        assert 'Stdev' in output
        assert 'ppm' in output


class TestMovingAverage:
    def test_output_length_matches_input(self):
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = moving_average(data, window=3)
        assert len(result) == len(data)

    def test_leading_nans(self):
        data = [1.0, 2.0, 3.0, 4.0, 5.0]
        result = moving_average(data, window=3)
        assert np.isnan(result[0])
        assert np.isnan(result[1])
        assert not np.isnan(result[2])

    def test_median_method(self):
        data = [1.0, 3.0, 2.0, 4.0, 6.0]
        result = moving_average(data, window=3, method='median')
        assert result[2] == pytest.approx(2.0)  # median of [1, 3, 2]

    def test_mean_method(self):
        data = [1.0, 2.0, 3.0]
        result = moving_average(data, window=3, method='mean')
        assert result[2] == pytest.approx(2.0)

    def test_invalid_method_raises(self):
        with pytest.raises(ValueError, match="Unknown method"):
            moving_average([1, 2, 3], window=2, method='invalid')


class TestResToPt1000:
    def test_zero_celsius(self):
        # PT1000 resistance at 0C is 1000 ohms
        assert res_to_pt1000(1000.0) == pytest.approx(0.0, abs=0.01)

    def test_positive_temperature(self):
        # ~1039.7 ohms at ~10C
        temp = res_to_pt1000(1039.7)
        assert 9.5 < temp < 10.5

    def test_room_temperature(self):
        # ~1097.3 ohms at ~25C
        temp = res_to_pt1000(1097.3)
        assert 24.5 < temp < 25.5

    def test_boiling_point(self):
        # ~1385.1 ohms at ~100C
        temp = res_to_pt1000(1385.1)
        assert 99.5 < temp < 100.5

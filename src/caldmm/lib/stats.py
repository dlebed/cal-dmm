"""Statistical analysis utilities for measurement data."""

import math
from dataclasses import dataclass

import numpy as np
from prefixed import Float


@dataclass
class StatResult:
    """Result of compute_stats()."""
    mean: float
    median: float
    stdev: float
    stdev_ppm: float
    sigma3_plus: float
    sigma3_minus: float
    min: float
    max: float
    pp: float
    pp_ppm: float
    count: int


def compute_stats(samples) -> StatResult:
    """Compute standard measurement statistics on a 1-D array of samples.

    Returns a StatResult with mean, median, stdev, min/max, peak-to-peak, and
    PPM-referenced values.
    """
    arr = np.asarray(samples, dtype=np.float64)
    mean = float(np.mean(arr))
    median = float(np.median(arr))
    stdev = float(np.std(arr))
    value_min = float(np.min(arr))
    value_max = float(np.max(arr))
    pp = value_max - value_min

    if mean != 0:
        stdev_ppm = (stdev / abs(mean)) * 1e6
        pp_ppm = (pp / abs(mean)) * 1e6
    else:
        stdev_ppm = 0.0
        pp_ppm = 0.0

    return StatResult(
        mean=mean,
        median=median,
        stdev=stdev,
        stdev_ppm=stdev_ppm,
        sigma3_plus=mean + stdev * 3,
        sigma3_minus=mean - stdev * 3,
        min=value_min,
        max=value_max,
        pp=pp,
        pp_ppm=pp_ppm,
        count=len(arr),
    )


def print_stats(stats: StatResult, unit: str = '') -> None:
    """Print formatted statistics to stdout (matches legacy do_stats output)."""
    print(f"\tMean: {Float(stats.mean):.9h}{unit}")
    print(f"\tMedian: {Float(stats.median):.9h}{unit}")
    print(f"\tStdev: {Float(stats.stdev):.3h}{unit} ({stats.stdev_ppm:05.2f} ppm)")
    print(f"\t3-sigma: {Float(stats.stdev * 3):.3h}{unit} ({stats.stdev_ppm * 3:05.2f} ppm)")
    print(f"\tMin: {Float(stats.min):.9h}{unit}")
    print(f"\tMax: {Float(stats.max):.9h}{unit}")
    print(f"\tDelta (max-min): {Float(stats.pp):.3h}{unit} ({stats.pp_ppm:.1f} ppm)")


def moving_average(samples, window: int, method: str = 'median'):
    """Compute a rolling moving average (median or mean) over a 1-D array.

    Returns a numpy array of the same length (leading values are NaN).
    """
    import pandas as pd
    series = pd.Series(samples)
    if method == 'median':
        return series.rolling(window).median().to_numpy()
    elif method == 'mean':
        return series.rolling(window).mean().to_numpy()
    else:
        raise ValueError(f"Unknown method: {method!r}, expected 'median' or 'mean'")


def res_to_pt1000(r: float) -> float:
    """Convert PT1000 resistance (ohms) to temperature (Celsius).

    Uses the Callendar-Van Dusen equation (positive root, valid for T >= 0 C).
    """
    a = 3.9083e-03
    b = -5.775e-07
    return (-a + math.sqrt(a**2 - 4 * b * (1 - r / 1000))) / (2 * b)

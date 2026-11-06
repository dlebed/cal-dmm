"""The --adjust session record prints the per-point readings (2026-11-02)."""
from caldmm.apps import cal_verify as cv


def test_point_rows_lines_lists_check_and_adjust_rows():
    captures = {
        "as_measured": {"dcv-1v": {"applied": 1.0, "unit": "V",
                                   "reading": 0.9999932,
                                   "deviation_ppm": -6.8}},
        "adjusted": {"dcv-1v": {"applied": 1.0, "unit": "V",
                                "reading_before": 0.9999935,
                                "deviation_ppm": -6.5, "cal_count": 57}},
    }
    lines = cv._point_rows_lines(captures)
    text = "\n".join(lines)
    assert "check steps" in text and "adjust steps" in text
    assert "dev -6.80 ppm" in text and "CAL:COUN 57" in text


def test_point_rows_lines_empty_without_captures():
    assert cv._point_rows_lines({}) == []



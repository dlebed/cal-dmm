"""Tests for caldmm.lib.csv_io."""

import os
import tempfile
from datetime import datetime

import pandas as pd
import pytest

from caldmm.lib.csv_io import CsvWriter, load_csv, get_value_columns


@pytest.fixture
def tmp_csv(tmp_path):
    return str(tmp_path / "test.csv")


class TestCsvWriter:
    def test_creates_file_with_header(self, tmp_csv):
        with CsvWriter(tmp_csv, ['Value', 'Temp']) as w:
            pass
        with open(tmp_csv) as f:
            assert f.readline().strip() == 'Time;Value;Temp'

    def test_write_row(self, tmp_csv):
        ts = datetime(2026, 1, 1, 12, 0, 0)
        with CsvWriter(tmp_csv, ['Value', 'Temp']) as w:
            w.write_row({'Value': '1.234E+01', 'Temp': '22.5'}, ts=ts)
        with open(tmp_csv) as f:
            lines = f.readlines()
        assert len(lines) == 2
        parts = lines[1].strip().split(';')
        assert parts[0] == ts.isoformat()
        assert parts[1] == '1.234E+01'
        assert parts[2] == '22.5'

    def test_append_mode_no_duplicate_header(self, tmp_csv):
        with CsvWriter(tmp_csv, ['Value']) as w:
            w.write_row({'Value': '1.0'})

        with CsvWriter(tmp_csv, ['Value'], append=True) as w:
            w.write_row({'Value': '2.0'})

        with open(tmp_csv) as f:
            lines = f.readlines()
        # header + 2 data rows
        assert len(lines) == 3
        assert lines[0].startswith('Time')

    def test_missing_column_writes_empty(self, tmp_csv):
        with CsvWriter(tmp_csv, ['A', 'B']) as w:
            w.write_row({'A': '1'})
        with open(tmp_csv) as f:
            data_line = f.readlines()[1].strip()
        parts = data_line.split(';')
        assert parts[2] == ''  # B is missing

    def test_flush(self, tmp_csv):
        w = CsvWriter(tmp_csv, ['Value'])
        w.write_row({'Value': '42'})
        w.flush()
        assert os.path.getsize(tmp_csv) > 0
        w.close()


class TestLoadCsv:
    def _write_sample_csv(self, path):
        with open(path, 'w') as f:
            f.write("Time;Value_A;Value_B;Temp_Ext\n")
            for i in range(10):
                f.write(f"2026-01-01T00:00:{i:02d};{i:.1f};{i*2:.1f};22.0\n")

    def test_loads_all_rows(self, tmp_path):
        p = str(tmp_path / "data.csv")
        self._write_sample_csv(p)
        df = load_csv(p)
        assert len(df) == 10

    def test_skip_samples(self, tmp_path):
        p = str(tmp_path / "data.csv")
        self._write_sample_csv(p)
        df = load_csv(p, skip_samples=3)
        assert len(df) == 7

    def test_limit_samples(self, tmp_path):
        p = str(tmp_path / "data.csv")
        self._write_sample_csv(p)
        df = load_csv(p, limit_samples=5)
        assert len(df) == 5

    def test_skip_and_limit(self, tmp_path):
        p = str(tmp_path / "data.csv")
        self._write_sample_csv(p)
        df = load_csv(p, skip_samples=2, limit_samples=3)
        assert len(df) == 3

    def test_returns_dataframe(self, tmp_path):
        p = str(tmp_path / "data.csv")
        self._write_sample_csv(p)
        df = load_csv(p)
        assert isinstance(df, pd.DataFrame)


class TestGetValueColumns:
    def test_filters_value_columns(self):
        df = pd.DataFrame({
            'Time': [], 'Value_A': [], 'Value_B': [], 'Temp_Ext': []
        })
        assert get_value_columns(df) == ['Value_A', 'Value_B']

    def test_empty_when_no_value_columns(self):
        df = pd.DataFrame({'Time': [], 'Temp': []})
        assert get_value_columns(df) == []

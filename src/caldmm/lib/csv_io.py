"""CSV read/write helpers for caldmm measurement data.

The standard CSV format uses semicolons as delimiters and has a header row
with column names.  The first column is always an ISO-8601 timestamp.
"""

import os
from datetime import datetime
from typing import TextIO, Optional

import pandas as pd


class CsvWriter:
    """Writes measurement samples to a semicolon-delimited CSV file.

    Handles header creation on new files and append mode for existing ones.
    """

    def __init__(self, path: str, columns: list[str], *, append: bool = False):
        self._path = path
        self._columns = columns
        self._file: Optional[TextIO] = None

        mode = 'a' if append else 'w'
        self._file = open(path, mode)

        write_header = (not append) or (os.path.getsize(path) == 0)
        if write_header:
            header = 'Time;' + ';'.join(columns)
            self._file.write(header + '\n')

    def write_row(self, values: dict[str, str], ts: Optional[datetime] = None) -> None:
        """Write a single row.  *values* maps column names to formatted strings."""
        if ts is None:
            ts = datetime.now()
        parts = [ts.isoformat()]
        for col in self._columns:
            parts.append(values.get(col, ''))
        self._file.write(';'.join(parts) + '\n')

    def flush(self) -> None:
        if self._file:
            self._file.flush()

    def close(self) -> None:
        if self._file:
            self._file.close()
            self._file = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def load_csv(path: str, *, delimiter: str = ';', skip_samples: int = 0,
             limit_samples: int = -1) -> pd.DataFrame:
    """Load a caldmm CSV file into a pandas DataFrame.

    Applies optional sample skipping/limiting.
    """
    df = pd.read_csv(path, delimiter=delimiter)

    if skip_samples > 0:
        df = df.iloc[skip_samples:]

    if limit_samples > 0:
        df = df.iloc[:limit_samples]

    return df.reset_index(drop=True)


def get_value_columns(df: pd.DataFrame) -> list[str]:
    """Return the names of all Value_* columns in a DataFrame."""
    return [c for c in df.columns if c.startswith('Value')]

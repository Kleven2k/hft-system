from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Any

import pandas as pd


class ParquetWriter:
    def __init__(self, data_dir: str, filename: str = "top_of_book.parquet") -> None:
        self.path = Path(data_dir)
        self.path.mkdir(parents=True, exist_ok=True)
        self.filepath = self.path / filename

    def append_rows(self, rows: List[Dict[str, Any]]) -> None:
        if not rows:
            return

        new_df = pd.DataFrame(rows)

        if self.filepath.exists():
            old_df = pd.read_parquet(self.filepath)
            combined = pd.concat([old_df, new_df], ignore_index=True)
            combined.to_parquet(self.filepath, index=False)
        else:
            new_df.to_parquet(self.filepath, index=False)
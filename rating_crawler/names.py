# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

import pandas as pd


def load_issuers(excel_path: Path, column: str = "issuer_name_clean") -> list[tuple[int, str]]:
    df = pd.read_excel(excel_path)
    if column not in df.columns:
        raise SystemExit(f"excel missing column {column!r}, got {list(df.columns)}")
    seq_col = "issuer_seq" if "issuer_seq" in df.columns else None
    out: list[tuple[int, str]] = []
    for i, row in df.iterrows():
        name = str(row[column]).strip()
        if not name or name.lower() == "nan":
            continue
        seq = int(row[seq_col]) if seq_col is not None and pd.notna(row[seq_col]) else int(i) + 1
        out.append((seq, name))
    return out

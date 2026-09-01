# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path

import pandas as pd

TEMPLATE_COLUMNS = ["issuer_name"]


def parse_manual_names(text: str) -> list[tuple[int, str]]:
    raw = (text or "").replace("\n", ",").replace("，", ",")
    names = []
    seen = set()
    for part in raw.split(","):
        name = part.strip()
        if not name or name in seen:
            continue
        seen.add(name)
        names.append(name)
    return [(i + 1, n) for i, n in enumerate(names)]


def load_issuer_csv(path: Path) -> list[tuple[int, str]]:
    df = pd.read_csv(path)
    if df.empty:
        return []
    col = "issuer_name" if "issuer_name" in df.columns else df.columns[0]
    out: list[tuple[int, str]] = []
    seen = set()
    for i, val in enumerate(df[col].tolist(), 1):
        name = str(val).strip()
        if not name or name.lower() == "nan" or name in seen:
            continue
        seen.add(name)
        out.append((i, name))
    return out


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

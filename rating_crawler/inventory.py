# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd

COLUMNS = [
    "issuer_seq",
    "issuer_name",
    "source",
    "category",
    "agency",
    "title",
    "publish_date",
    "content_id",
    "doc_id",
    "detail_url",
    "pdf_url",
    "local_path",
    "file_size",
    "sha256",
    "status",
    "error",
    "duplicate_of",
]


def empty_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=COLUMNS)


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    slim = {k: row.get(k, "") for k in COLUMNS}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(slim, ensure_ascii=False) + "\n")


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    with path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def write_tables(rows: list[dict[str, Any]], out_dir: Path) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(rows, columns=COLUMNS) if rows else empty_frame()
    csv_path = out_dir / "inventory.csv"
    xlsx_path = out_dir / "inventory.xlsx"
    df.to_csv(csv_path, index=False, encoding="utf-8-sig")
    df.to_excel(xlsx_path, index=False)
    summary = (
        df.groupby(["source", "status"], dropna=False)
        .size()
        .reset_index(name="count")
        if not df.empty
        else pd.DataFrame(columns=["source", "status", "count"])
    )
    summary.to_csv(out_dir / "summary.csv", index=False, encoding="utf-8-sig")


def mark_duplicates(rows: list[dict[str, Any]]) -> None:
    by_hash: dict[str, str] = {}
    for row in rows:
        digest = row.get("sha256") or ""
        if not digest or row.get("status") != "ok":
            continue
        if digest in by_hash:
            row["duplicate_of"] = by_hash[digest]
        else:
            by_hash[digest] = row.get("content_id") or ""
            row["duplicate_of"] = ""

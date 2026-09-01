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
    "is_duplicate",
    "duplicate_of",
    "duplicate_group",
    "dup_reason",
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
    for col in (
        "content_id",
        "doc_id",
        "duplicate_of",
        "duplicate_group",
        "dup_reason",
        "local_path",
        "pdf_url",
        "detail_url",
        "is_duplicate",
    ):
        if col in df.columns:
            df[col] = df[col].fillna("").astype(str).replace({"nan": "", "None": ""})
    df.to_csv(out_dir / "inventory.csv", index=False, encoding="utf-8-sig")
    df.to_excel(out_dir / "inventory.xlsx", index=False)
    summary = (
        df.groupby(["source", "status"], dropna=False).size().reset_index(name="count")
        if not df.empty
        else pd.DataFrame(columns=["source", "status", "count"])
    )
    summary.to_csv(out_dir / "summary.csv", index=False, encoding="utf-8-sig")


def mark_duplicates(rows: list[dict[str, Any]]) -> None:
    by_hash: dict[str, str] = {}
    agency_by_hash: dict[str, str] = {}
    by_title: dict[str, str] = {}
    for row in rows:
        row["is_duplicate"] = "0"
        row["duplicate_of"] = row.get("duplicate_of") or ""
        row["duplicate_group"] = row.get("duplicate_group") or ""
        row["dup_reason"] = row.get("dup_reason") or ""
        digest = row.get("sha256") or ""
        title_key = "|".join(
            [
                str(row.get("issuer_name") or ""),
                str(row.get("title") or "").replace(".pdf", ""),
                str(row.get("publish_date") or "")[:10],
            ]
        )
        cid = str(row.get("content_id") or "")
        if digest and row.get("status") == "ok":
            if digest in by_hash:
                row["is_duplicate"] = "1"
                row["duplicate_of"] = by_hash[digest]
                row["duplicate_group"] = digest[:16]
                row["dup_reason"] = "same_file"
            else:
                by_hash[digest] = cid
                row["duplicate_group"] = digest[:16]
            if row.get("agency"):
                agency_by_hash.setdefault(digest, row["agency"])
        if title_key.strip("|") and cid:
            if title_key in by_title and by_title[title_key] != cid and row.get("is_duplicate") != "1":
                row["is_duplicate"] = "1"
                row["duplicate_of"] = by_title[title_key]
                row["dup_reason"] = row.get("dup_reason") or "same_title_date"
            else:
                by_title.setdefault(title_key, cid)
    for row in rows:
        digest = row.get("sha256") or ""
        if digest in agency_by_hash and not row.get("agency"):
            row["agency"] = agency_by_hash[digest]

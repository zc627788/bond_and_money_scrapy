# -*- coding: utf-8 -*-
from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import pandas as pd

# jsonl 全量字段（调试用）
JSONL_COLUMNS = [
    "issuer_seq",
    "issuer_name",
    "source",
    "category",
    "agency",
    "title",
    "publish_date",
    "query_start",
    "query_end",
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
    "dup_reason",
]

# 对外 CSV：货币网 / 中债各一份
CSV_COLUMNS = [
    "issuer_name",
    "query_start",
    "query_end",
    "category",
    "agency",
    "title",
    "publish_date",
    "detail_url",
    "pdf_url",
    "local_path",
    "error",
    "is_duplicate",
    "duplicate_of",
    "dup_reason",
]


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    slim = {k: row.get(k, "") for k in JSONL_COLUMNS}
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


def _clean_title(title: str) -> str:
    t = str(title or "").strip()
    if t.lower().endswith(".pdf"):
        t = t[:-4]
    return t


def write_tables(rows: list[dict[str, Any]], out_dir: Path, query_start: str = "", query_end: str = "") -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    for row in rows:
        if not row.get("query_start"):
            row["query_start"] = query_start
        if not row.get("query_end"):
            row["query_end"] = query_end
        row["title"] = _clean_title(row.get("title") or "")
        if row.get("status") == "ok":
            row["error"] = ""

    def to_csv(part: list[dict[str, Any]], name: str) -> None:
        df = pd.DataFrame(part)
        for col in CSV_COLUMNS:
            if col not in df.columns:
                df[col] = ""
        df = df[CSV_COLUMNS]
        for col in CSV_COLUMNS:
            df[col] = df[col].fillna("").astype(str).replace({"nan": "", "None": ""})
        df.to_csv(out_dir / name, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_ALL)

    money = [r for r in rows if r.get("source") == "chinamoney"]
    bond = [r for r in rows if r.get("source") == "chinabond"]
    to_csv(money, "chinamoney.csv")
    to_csv(bond, "chinabond.csv")

    summary_rows = []
    for src, part in (("chinamoney", money), ("chinabond", bond)):
        ok = sum(1 for r in part if r.get("status") == "ok")
        fail = sum(1 for r in part if r.get("status") == "fail")
        locked = sum(1 for r in part if r.get("status") == "locked")
        dup = sum(1 for r in part if str(r.get("is_duplicate")) == "1")
        summary_rows.append(
            {"source": src, "rows": len(part), "ok": ok, "fail": fail, "locked": locked, "duplicate": dup}
        )
    pd.DataFrame(summary_rows).to_csv(out_dir / "summary.csv", index=False, encoding="utf-8-sig")


def mark_duplicates(rows: list[dict[str, Any]]) -> None:
    """只把「文件字节相同」标成重复。披露日期不同也可以重复，理由里写清楚。"""
    by_hash: dict[str, dict[str, Any]] = {}
    agency_by_hash: dict[str, str] = {}
    for row in rows:
        row["is_duplicate"] = "0"
        row["duplicate_of"] = ""
        row["dup_reason"] = ""
        digest = row.get("sha256") or ""
        if not digest or row.get("status") != "ok":
            continue
        cid = str(row.get("content_id") or "")
        if digest in by_hash:
            first = by_hash[digest]
            row["is_duplicate"] = "1"
            row["duplicate_of"] = str(first.get("content_id") or "")
            d1 = str(first.get("publish_date") or "")[:10]
            d2 = str(row.get("publish_date") or "")[:10]
            s1 = first.get("source") or ""
            s2 = row.get("source") or ""
            if d1 and d2 and d1 != d2:
                row["dup_reason"] = (
                    f"文件内容相同（哈希一致），披露日期不同：主份 {s1} {d1} / 本条 {s2} {d2}"
                )
            else:
                row["dup_reason"] = f"文件内容相同（哈希一致），与 {s1} 为同一份 PDF"
        else:
            by_hash[digest] = row
        if row.get("agency"):
            agency_by_hash.setdefault(digest, row["agency"])
    for row in rows:
        digest = row.get("sha256") or ""
        if digest in agency_by_hash and not row.get("agency"):
            row["agency"] = agency_by_hash[digest]

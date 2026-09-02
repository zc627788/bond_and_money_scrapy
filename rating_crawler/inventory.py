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
    "error_code",
    "http_status",
    "is_duplicate",
    "duplicate_of",
    "dup_reason",
]

STATUS_CN = {
    "ok": "成功",
    "fail": "失败",
    "locked": "需登录",
    "not_pdf": "失败",
    "listed": "仅有目录",
    "no_file": "无附件",
    "empty": "无记录",
}
SOURCE_CN = {
    "chinamoney": "中国货币网",
    "chinabond": "中国债券信息网",
}
ERROR_TYPE_CN = {
    "timeout": "超时",
    "link": "链接问题",
    "login": "登录问题",
    "missing": "链接不存在",
    "rate": "请求过频",
    "server": "服务器错误",
    "not_pdf": "不是PDF",
    "empty": "空响应",
    "no_url": "无下载地址",
    "error": "其他",
}

CSV_CN = [
    ("issuer_name", "公司名称"),
    ("query_start", "查询起始日"),
    ("query_end", "查询截止日"),
    ("category", "栏目"),
    ("agency", "评级机构"),
    ("title", "文件标题"),
    ("publish_date", "披露日期"),
    ("detail_url", "网站详情页"),
    ("pdf_url", "文件下载地址"),
    ("local_path", "电脑里的文件"),
    ("status_cn", "结果"),
    ("error_type_cn", "失败类型"),
    ("error", "失败说明"),
    ("http_cn", "网络代码"),
    ("dup_cn", "是否重复文件"),
    ("dup_reason", "重复说明"),
]
INV_CN = [
    ("source_cn", "来源"),
    ("issuer_name", "公司名称"),
    ("found_for", "还出现在这些公司"),
    ("category", "栏目"),
    ("agency", "评级机构"),
    ("title", "文件标题"),
    ("publish_date", "披露日期"),
    ("detail_url", "网站详情页"),
    ("pdf_url", "文件下载地址"),
    ("local_path", "电脑里的文件"),
    ("status_cn", "结果"),
    ("error_type_cn", "失败类型"),
    ("error", "失败说明"),
    ("http_cn", "网络代码"),
    ("dup_cn", "是否重复文件"),
    ("dup_reason", "重复说明"),
]
SUM_CN = ["来源", "总条数", "成功", "失败", "需登录", "重复文件"]

README_TXT = """这个文件夹是下载结果。

请先用 Excel 打开：
  评级报告清单.xlsx

里面有四张表：货币网、中债、总清单、汇总。

不会用 Excel 也可以打开同名的 CSV：
  货币网.csv
  中债.csv
  总清单.csv
  汇总.csv

PDF 在软件旁边的 downloads 文件夹里，按「网站 / 公司名称」分好了。

下面这些是程序自己记进度用的，不用打开：
  progress.json  records.jsonl  state.json
"""


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
            row["error_code"] = ""

    money = [r for r in rows if r.get("source") == "chinamoney"]
    bond = [r for r in rows if r.get("source") == "chinabond"]
    inv = _inventory_rows(rows)
    money_b = [_business_view(r) for r in money]
    bond_b = [_business_view(r) for r in bond]
    inv_b = [_business_view(r, inventory=True) for r in inv]
    _write_cn_csv(out_dir / "货币网.csv", money_b, CSV_CN)
    _write_cn_csv(out_dir / "中债.csv", bond_b, CSV_CN)
    _write_cn_csv(out_dir / "总清单.csv", inv_b, INV_CN)
    _write_cn_csv(out_dir / "chinamoney.csv", money_b, CSV_CN)
    _write_cn_csv(out_dir / "chinabond.csv", bond_b, CSV_CN)
    _write_cn_csv(out_dir / "inventory.csv", inv_b, INV_CN)

    summary = []
    for src, part, label in (("chinamoney", money, "中国货币网"), ("chinabond", bond, "中国债券信息网")):
        summary.append(
            {
                "来源": label,
                "总条数": len(part),
                "成功": sum(1 for r in part if r.get("status") == "ok"),
                "失败": sum(1 for r in part if r.get("status") in {"fail", "not_pdf"}),
                "需登录": sum(1 for r in part if r.get("status") == "locked"),
                "重复文件": sum(1 for r in part if str(r.get("is_duplicate")) == "1"),
            }
        )
    sum_df = pd.DataFrame(summary)
    sum_df.to_csv(out_dir / "汇总.csv", index=False, encoding="utf-8-sig", quoting=csv.QUOTE_ALL)
    sum_df.to_csv(out_dir / "summary.csv", index=False, encoding="utf-8-sig", quoting=csv.QUOTE_ALL)
    _write_xlsx(out_dir / "评级报告清单.xlsx", money_b, bond_b, inv_b, sum_df)
    (out_dir / "请先看这里.txt").write_text(README_TXT, encoding="utf-8")


def _business_view(row: dict[str, Any], *, inventory: bool = False) -> dict[str, Any]:
    http_s = row.get("http_status") or 0
    try:
        http_n = int(http_s)
    except (TypeError, ValueError):
        http_n = 0
    out = dict(row)
    out["source_cn"] = SOURCE_CN.get(str(row.get("source") or ""), str(row.get("source") or ""))
    out["status_cn"] = STATUS_CN.get(str(row.get("status") or ""), str(row.get("status") or ""))
    out["error_type_cn"] = "" if row.get("status") == "ok" else ERROR_TYPE_CN.get(str(row.get("error_code") or ""), str(row.get("error_code") or ""))
    out["http_cn"] = str(http_n) if http_n else ""
    out["dup_cn"] = "是" if str(row.get("is_duplicate")) == "1" else "否"
    return out


def _write_cn_csv(path: Path, rows: list[dict[str, Any]], spec: list[tuple[str, str]]) -> None:
    keys = [k for k, _ in spec]
    headers = [h for _, h in spec]
    df = pd.DataFrame(rows)
    for k in keys:
        if k not in df.columns:
            df[k] = ""
    if df.empty:
        df = pd.DataFrame(columns=keys)
    df = df[keys]
    df.columns = headers
    for col in headers:
        df[col] = df[col].fillna("").astype(str).replace({"nan": "", "None": ""})
    df.to_csv(path, index=False, encoding="utf-8-sig", quoting=csv.QUOTE_ALL)


def _write_xlsx(
    path: Path,
    money: list[dict[str, Any]],
    bond: list[dict[str, Any]],
    inv: list[dict[str, Any]],
    summary: pd.DataFrame,
) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as xw:
        def sheet(rows: list[dict[str, Any]], spec: list[tuple[str, str]], name: str) -> None:
            keys = [k for k, _ in spec]
            headers = [h for _, h in spec]
            df = pd.DataFrame(rows)
            for k in keys:
                if k not in df.columns:
                    df[k] = ""
            if df.empty:
                df = pd.DataFrame(columns=keys)
            df = df[keys]
            df.columns = headers
            df.to_excel(xw, sheet_name=name, index=False)

        sheet(money, CSV_CN, "货币网")
        sheet(bond, CSV_CN, "中债")
        sheet(inv, INV_CN, "总清单")
        summary.to_excel(xw, sheet_name="汇总", index=False)


def _inventory_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """同一 source+content_id 只留一条；失败也写入。"""
    rank = {"ok": 3, "locked": 2, "fail": 2, "not_pdf": 1, "listed": 0, "empty": 0}
    uniq: dict[str, dict[str, Any]] = {}
    found: dict[str, list[str]] = {}
    for row in rows:
        if row.get("status") == "empty":
            continue
        cid = str(row.get("content_id") or "")
        if not cid:
            continue
        key = f"{row.get('source')}|{cid}"
        names = found.setdefault(key, [])
        issuer = str(row.get("issuer_name") or "")
        if issuer and issuer not in names:
            names.append(issuer)
        prev = uniq.get(key)
        if not prev or rank.get(str(row.get("status") or ""), 0) > rank.get(str(prev.get("status") or ""), 0):
            uniq[key] = dict(row)
    out = []
    for key, row in uniq.items():
        names = found.get(key) or []
        row["found_for"] = "；".join(names)
        if names and not row.get("issuer_name"):
            row["issuer_name"] = names[0]
        out.append(row)
    return out


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

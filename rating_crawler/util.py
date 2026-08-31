# -*- coding: utf-8 -*-
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any

WIN_BAD = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
SPACE = re.compile(r"\s+")

AGENCY_ALIASES = [
    "东方金诚国际信用评估有限公司",
    "中诚信国际信用评级有限责任公司",
    "中诚信证券评估有限公司",
    "联合资信评估股份有限公司",
    "联合信用评级有限公司",
    "大公国际资信评估有限公司",
    "上海新世纪资信评估投资服务有限公司",
    "中证鹏元资信评估股份有限公司",
    "远东资信评估有限公司",
    "中债资信评估有限责任公司",
    "安永华明会计师事务所",
    "东方金诚",
    "中诚信国际",
    "中诚信证评",
    "中诚信",
    "联合资信",
    "联合信用",
    "大公国际",
    "大公资信",
    "新世纪",
    "中证鹏元",
    "鹏元资信",
    "远东资信",
    "中债资信",
    "标普",
    "穆迪",
    "惠誉",
]


def today_str() -> str:
    return date.today().isoformat()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path, chunk: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            buf = f.read(chunk)
            if not buf:
                break
            h.update(buf)
    return h.hexdigest()


def sanitize_filename(name: str, max_len: int = 120) -> str:
    name = unicodedata.normalize("NFKC", name or "")
    name = WIN_BAD.sub("_", name)
    name = SPACE.sub(" ", name).strip(" ._")
    if not name:
        name = "untitled"
    if len(name) > max_len:
        name = name[:max_len].rstrip(" ._")
    return name


def issuer_dirname(issuer: str) -> str:
    return sanitize_filename(issuer, max_len=80)


def guess_agency(title: str, prefix: str = "") -> str:
    if prefix:
        return prefix.strip()
    title = title or ""
    for name in AGENCY_ALIASES:
        if name in title:
            return name
    return ""


def guess_ext(suffix: str, magic: bytes) -> str:
    s = (suffix or "").lower().lstrip(".")
    if s in {"pdf", "doc", "docx", "xls", "xlsx", "zip", "rar"}:
        return s
    if magic.startswith(b"%PDF"):
        return "pdf"
    if magic[:2] == b"PK":
        return "zip"
    return s or "bin"


def is_pdf(data: bytes) -> bool:
    return data[:4] == b"%PDF"

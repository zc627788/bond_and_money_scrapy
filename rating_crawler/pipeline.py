# -*- coding: utf-8 -*-
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable, Optional

from .chinabond import ChinaBondClient
from .chinamoney import ChinaMoneyClient
from .http import BrowserSession
from .inventory import append_jsonl, load_jsonl, mark_duplicates, write_tables
from .util import guess_ext, is_pdf, issuer_dirname, sanitize_filename, sha256_bytes, sha256_file


class Crawler:
    def __init__(self, settings: dict[str, Any], root: Path):
        self.settings = settings
        self.root = root
        delay = float(settings.get("delay_seconds", 1.4))
        retries = int(settings.get("max_retries", 5))
        self.download_dir = root / settings.get("download_dir", "downloads")
        self.output_dir = root / settings.get("output_dir", "output")
        self.records_path = self.output_dir / "records.jsonl"
        self.state_path = self.output_dir / "state.json"
        self.http_cm = BrowserSession(delay=delay, max_retries=retries)
        self.http_cb = BrowserSession(delay=delay, max_retries=retries)
        cm = settings.get("chinamoney") or {}
        cb = settings.get("chinabond") or {}
        self.cm = ChinaMoneyClient(
            self.http_cm,
            page_size=int(cm.get("page_size", 15)),
            start_date=str(cm.get("start_date", "2006-08-01")),
        )
        self.cb = ChinaBondClient(
            self.http_cb,
            page_size=int(cb.get("page_size", 20)),
            start_date=str(cb.get("start_date", "2006-01-01")),
            parent_chnl_name=cb.get("parent_chnl_name", "fxyfxdh_zqzl"),
            child_chnl_desc=cb.get("child_chnl_desc", "评级文件"),
            exclude_parent_chnl_names=cb.get("exclude_parent_chnl_names") or [],
            jrzq_chnl_name=cb.get("jrzq_chnl_name", ""),
        )
        self.cm_categories = cm.get("categories") or []
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)

    def load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            return {"done": [], "failed": {}}
        return json.loads(self.state_path.read_text(encoding="utf-8"))

    def save_state(self, state: dict[str, Any]) -> None:
        self.state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")

    def run(
        self,
        issuers: list[tuple[int, str]],
        *,
        sources: Iterable[str] = ("chinamoney", "chinabond"),
        download: bool = True,
        resume: bool = True,
    ) -> None:
        sources = tuple(sources)
        state = self.load_state() if resume else {"done": [], "failed": {}}
        done = set(state.get("done") or [])
        for seq, name in issuers:
            if name in done:
                print(f"[skip] {seq} {name} already done")
                continue
            print(f"\n===== [{seq}] {name} =====")
            try:
                self._run_one(seq, name, sources=sources, download=download)
                done.add(name)
                state["done"] = sorted(done)
                if name in state.get("failed", {}):
                    del state["failed"][name]
                self.save_state(state)
            except Exception as e:
                print(f"  [fail] {name}: {type(e).__name__}: {e}")
                state.setdefault("failed", {})[name] = f"{type(e).__name__}: {e}"
                self.save_state(state)
            self._flush_tables()
        self._flush_tables()
        print("\n完成。清单:", self.output_dir / "inventory.xlsx")

    def _run_one(self, seq: int, name: str, *, sources: tuple[str, ...], download: bool) -> None:
        items: list[dict[str, Any]] = []
        if "chinamoney" in sources:
            for cat in self.cm_categories:
                print(f"  chinamoney {cat.get('label')} ...")
                rows = self.cm.search_category(
                    name,
                    scnd=str(cat["scnd"]),
                    channel_path=str(cat["channel_path"]),
                    label=str(cat["label"]),
                )
                print(f"    {len(rows)} 条")
                items.extend(rows)
        if "chinabond" in sources:
            print("  chinabond 评级文件 ...")
            rows = self.cb.search(name)
            print(f"    {len(rows)} 条")
            items.extend(rows)

        if not items:
            print("  无记录")
            append_jsonl(
                self.records_path,
                {
                    "issuer_seq": seq,
                    "issuer_name": name,
                    "source": ",".join(sources),
                    "category": "",
                    "title": "",
                    "status": "empty",
                    "error": "",
                },
            )
            return

        for item in items:
            row = self._to_row(seq, name, item)
            if download and item.get("pdf_url") and not item.get("locked"):
                self._download_row(row, item)
            elif item.get("locked"):
                row["status"] = "locked"
                row["error"] = "quanXianMa"
            elif not item.get("pdf_url"):
                row["status"] = "no_file"
            else:
                row["status"] = "listed"
            append_jsonl(self.records_path, row)
            print(
                f"    [{row['status']}] {row['source']} {row['publish_date']} "
                f"{row['agency']} {row['title'][:60]}"
            )

    def _to_row(self, seq: int, name: str, item: dict[str, Any]) -> dict[str, Any]:
        return {
            "issuer_seq": seq,
            "issuer_name": name,
            "source": item.get("source", ""),
            "category": item.get("category", ""),
            "agency": item.get("agency", ""),
            "title": item.get("title", ""),
            "publish_date": item.get("publish_date", ""),
            "content_id": item.get("content_id", ""),
            "doc_id": item.get("doc_id", ""),
            "detail_url": item.get("detail_url", ""),
            "pdf_url": item.get("pdf_url", ""),
            "local_path": "",
            "file_size": 0,
            "sha256": "",
            "status": "",
            "error": "",
            "duplicate_of": "",
        }

    def _download_row(self, row: dict[str, Any], item: dict[str, Any]) -> None:
        dest = self._dest_path(row, item)
        if dest.exists() and dest.stat().st_size > 1000:
            row["local_path"] = str(dest.relative_to(self.root))
            row["file_size"] = dest.stat().st_size
            row["sha256"] = sha256_file(dest)
            row["status"] = "ok"
            return
        try:
            client = self.cm if item.get("source") == "chinamoney" else self.cb
            data, remote_name = client.download(item)
            ext = guess_ext(item.get("suffix") or Path(remote_name).suffix, data)
            if dest.suffix.lower() != f".{ext}":
                dest = dest.with_suffix(f".{ext}")
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)
            row["local_path"] = str(dest.relative_to(self.root))
            row["file_size"] = len(data)
            row["sha256"] = sha256_bytes(data)
            row["status"] = "ok" if (ext != "pdf" or is_pdf(data)) else "not_pdf"
            if row["status"] != "ok":
                row["error"] = "magic mismatch"
        except Exception as e:
            row["status"] = "fail"
            row["error"] = f"{type(e).__name__}: {e}"

    def _dest_path(self, row: dict[str, Any], item: dict[str, Any]) -> Path:
        issuer_dir = self.download_dir / row["source"] / issuer_dirname(row["issuer_name"])
        date_s = (row["publish_date"] or "undated").replace("-", "")
        agency = sanitize_filename(row["agency"] or "未知机构", max_len=20)
        title = sanitize_filename(row["title"] or item.get("content_id") or "report", max_len=70)
        cid = sanitize_filename(str(item.get("content_id") or ""), max_len=24)
        ext = (item.get("suffix") or "pdf").lstrip(".")
        fname = f"{date_s}_{agency}_{title}_{cid}.{ext}"
        return issuer_dir / fname

    def _flush_tables(self) -> None:
        rows = load_jsonl(self.records_path)
        rows = _last_wins(rows)
        mark_duplicates(rows)
        write_tables(rows, self.output_dir)


def _last_wins(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    extras: list[dict[str, Any]] = []
    for row in rows:
        key = f"{row.get('source')}|{row.get('content_id')}|{row.get('issuer_name')}"
        if not row.get("content_id"):
            extras.append(row)
            continue
        indexed[key] = row
    return extras + list(indexed.values())

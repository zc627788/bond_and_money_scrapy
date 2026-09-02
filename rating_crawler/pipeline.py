# -*- coding: utf-8 -*-
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Iterable, Optional

from .chinabond import ChinaBondClient
from .chinamoney import ChinaMoneyClient
from .http import BrowserSession
from .inventory import append_jsonl, load_jsonl, mark_duplicates, write_tables
from .progress import Progress
from .proxy import ProxyPool
from .util import guess_ext, is_pdf, issuer_dirname, sanitize_filename, sha256_bytes, sha256_file


def log(msg: str) -> None:
    print(msg, flush=True)


class Crawler:
    def __init__(self, settings: dict[str, Any], root: Path, hooks: Optional[dict] = None):
        self.settings = settings
        self.root = root
        self.hooks = hooks or {}
        self._pause = threading.Event()
        self._pause.set()
        delay = float(settings.get("delay_seconds", 0))
        retries = int(settings.get("max_retries", 4))
        self.workers = max(1, int(settings.get("workers", 12)))
        self.issuer_workers = max(1, int(settings.get("issuer_workers", 4)))
        self.max_pages = int(settings.get("max_pages") or 0)
        self.download_dir = root / settings.get("download_dir", "downloads")
        self.output_dir = root / settings.get("output_dir", "output")
        self.records_path = self.output_dir / "records.jsonl"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.download_dir.mkdir(parents=True, exist_ok=True)

        px = settings.get("proxy") or {}
        self.proxy_pool: Optional[ProxyPool] = None
        if px.get("enabled") and px.get("api"):
            self.proxy_pool = ProxyPool(
                api=str(px["api"]),
                max_extract=int(px.get("max_extract", 50)),
                refresh_seconds=int(px.get("refresh_seconds", 180)),
            )

        self.http_cm = BrowserSession(delay=delay, max_retries=retries, proxy_pool=self.proxy_pool)
        self.http_cb = BrowserSession(delay=delay, max_retries=retries, proxy_pool=self.proxy_pool)
        cm = settings.get("chinamoney") or {}
        cb = settings.get("chinabond") or {}
        self.cm = ChinaMoneyClient(
            self.http_cm,
            page_size=int(cm.get("page_size", 15)),
            start_date=str(cm.get("start_date", "1990-01-01")),
        )
        self.cb = ChinaBondClient(
            self.http_cb,
            page_size=int(cb.get("page_size", 50)),
            start_date=str(cb.get("start_date", "1990-01-01")),
            parent_chnl_name=cb.get("parent_chnl_name", "fxyfxdh_zqzl"),
            child_chnl_desc=cb.get("child_chnl_desc", "评级文件"),
            exclude_parent_chnl_names=cb.get("exclude_parent_chnl_names") or [],
            jrzq_chnl_name=cb.get("jrzq_chnl_name", ""),
        )
        self.cm_categories = cm.get("categories") or []
        self.progress = Progress(self.output_dir / "progress.json", self.output_dir / "state.json")
        self._jsonl_lock = threading.Lock()
        self._flush_lock = threading.Lock()
        self._index = self._load_index()

    def log(self, msg: str) -> None:
        print(msg, flush=True)
        cb = self.hooks.get("log")
        if cb:
            cb(msg)

    def pause(self) -> None:
        self._pause.clear()
        self.log("已暂停")

    def resume(self) -> None:
        self._pause.set()
        self.log("继续")

    def wait_if_paused(self) -> None:
        self._pause.wait()

    def _hook(self, name: str, payload: dict[str, Any]) -> None:
        cb = self.hooks.get(name)
        if cb:
            cb(payload)

    def _load_index(self) -> dict[str, dict[str, Any]]:
        idx: dict[str, dict[str, Any]] = {}
        for row in load_jsonl(self.records_path):
            key = _row_key(row)
            if key:
                idx[key] = row
        return idx

    def _write_row(self, row: dict[str, Any]) -> None:
        with self._jsonl_lock:
            append_jsonl(self.records_path, row)
            key = _row_key(row)
            if key:
                self._index[key] = row

    def run(
        self,
        issuers: list[tuple[int, str]],
        *,
        sources: Iterable[str] = ("chinamoney", "chinabond"),
        download: bool = True,
        resume: bool = True,
    ) -> None:
        sources = tuple(sources)
        todo = []
        for seq, name in issuers:
            if resume and self.progress.is_done(name):
                counts = self._issuer_file_counts(name)
                extra = f"，已有 {counts['ok']} 个文件" if counts["ok"] else "，此前无文件"
                if counts["fail"]:
                    extra += f"，失败 {counts['fail']}"
                if counts["locked"]:
                    extra += f"，锁定 {counts['locked']}"
                self.log(f"[skip] {seq} {name} already done{extra}")
                self._hook(
                    "skipped",
                    {
                        "seq": seq,
                        "name": name,
                        "ok": counts["ok"],
                        "fail": counts["fail"],
                        "skip": counts["skip"],
                        "locked": counts["locked"],
                    },
                )
                continue
            todo.append((seq, name))
        n = max(1, min(self.issuer_workers, len(todo) or 1))
        self.log(f"公司并发 {n}，每家下载线程 {self.workers}（代理池共享）")

        def _one(pair: tuple[int, str]) -> None:
            seq, name = pair
            self.wait_if_paused()
            self.log(f"\n===== [{seq}] {name} =====")
            self._hook("issuer", {"seq": seq, "name": name, "phase": "start"})
            self.progress.issuer(name, seq)
            self.progress.mark_issuer(name, "running")
            try:
                complete = self._run_one(seq, name, sources=sources, download=download)
                self.progress.mark_issuer(name, "done" if complete else "failed")
                self._hook(
                    "issuer",
                    {"seq": seq, "name": name, "phase": "done" if complete else "failed"},
                )
                if not complete:
                    self.log(f"  [{seq}] 未全部完成，已写入断点，下次续跑")
            except Exception as e:
                self.log(f"  [fail] {seq} {name}: {type(e).__name__}: {e}")
                self.progress.mark_issuer(name, "failed")
                self._hook("issuer", {"seq": seq, "name": name, "phase": "failed"})
            self._flush_tables()

        if not todo:
            self._flush_tables()
        elif n == 1:
            for pair in todo:
                _one(pair)
        else:
            with ThreadPoolExecutor(max_workers=n) as pool:
                futs = [pool.submit(_one, pair) for pair in todo]
                for fut in as_completed(futs):
                    fut.result()
        self._flush_tables()
        self.log(f"\n完成。清单: {self.output_dir / 'chinamoney.csv'}  {self.output_dir / 'chinabond.csv'}")
        self.log(f"断点: {self.output_dir / 'progress.json'}")

    def _run_one(self, seq: int, name: str, *, sources: tuple[str, ...], download: bool) -> bool:
        pending: list[dict[str, Any]] = []
        if "chinamoney" in sources:
            for cat in self.cm_categories:
                try:
                    pending.extend(self._list_chinamoney(seq, name, cat))
                except Exception as e:
                    self.log(f"  chinamoney {cat.get('label')} 列表失败，稍后断点续: {type(e).__name__}: {e}")
        if "chinabond" in sources:
            try:
                pending.extend(self._list_chinabond(seq, name))
            except Exception as e:
                self.log(f"  chinabond 列表失败，稍后断点续: {type(e).__name__}: {e}")

        if not pending:
            snap = self.progress.snapshot(name)
            already = any(j.get("listed") for j in (snap.get("jobs") or {}).values())
            if not already:
                self.log("  无记录")
                self._write_row(
                    {
                        "issuer_seq": seq,
                        "issuer_name": name,
                        "source": ",".join(sources),
                        "status": "empty",
                    }
                )
            self._hook("listed", {"issuer": name, "total": 0, "done": 0})
            return self._lists_complete(name, sources)

        if download:
            self._download_many(name, pending)
        return self._lists_complete(name, sources)

    def _issuer_file_counts(self, name: str) -> dict[str, int]:
        jobs = (self.progress.snapshot(name).get("jobs") or {}).values()
        return {
            "ok": sum(int(j.get("download_ok") or 0) for j in jobs),
            "fail": sum(int(j.get("download_fail") or 0) for j in jobs),
            "skip": sum(int(j.get("download_skip") or 0) for j in jobs),
            "locked": sum(int(j.get("locked") or 0) for j in jobs),
        }

    def _lists_complete(self, name: str, sources: tuple[str, ...]) -> bool:
        jobs = (self.progress.snapshot(name).get("jobs") or {})
        need = []
        if "chinamoney" in sources:
            for cat in self.cm_categories:
                need.append(f"chinamoney|{cat.get('label')}")
        if "chinabond" in sources:
            need.append("chinabond|评级文件")
        return all((jobs.get(k) or {}).get("list_done") for k in need)

    def _list_chinamoney(self, seq: int, name: str, cat: dict[str, Any]) -> list[dict[str, Any]]:
        label = str(cat.get("label") or "债项评级报告")
        job_key = f"chinamoney|{label}"
        job = self.progress.job(name, job_key)
        out: list[dict[str, Any]] = []
        if job.get("list_done"):
            self.log(f"  chinamoney {label} 列表已完成 total={job.get('list_total')} listed={job.get('listed')}")
            return self._pending_from_index(name, "chinamoney", label)
        start_page = int(job.get("next_page") or 1)
        self.log(f"  chinamoney {label} 从第 {start_page} 页 ...")
        last_total = 0
        for pack in self.cm.iter_pages(
            name,
            scnd=str(cat["scnd"]),
            channel_path=str(cat["channel_path"]),
            label=label,
            start_page=start_page,
            max_pages=self.max_pages,
        ):
            self.wait_if_paused()
            added = 0
            for item in pack["items"]:
                row = self._to_row(seq, name, item)
                key = _row_key(row)
                prev = self._index.get(key) if key else None
                if prev and prev.get("status") in {"ok", "listed", "locked", "no_file"}:
                    out.append({**item, "_row": prev})
                    continue
                row["status"] = "listed"
                self._write_row(row)
                added += 1
                out.append({**item, "_row": row})
            last_total = pack["total"]
            self.progress.mark_page(
                name,
                job_key,
                page=pack["page"],
                total=pack["total"],
                pages=pack["pages"],
                added=added,
            )
            self.log(
                f"    页 {pack['page']}/{pack['pages']} 本页新 {added} 条，接口总数 {pack['total']}"
            )
        self.progress.mark_list_done(name, job_key, last_total)
        return out

    def _list_chinabond(self, seq: int, name: str) -> list[dict[str, Any]]:
        job_key = "chinabond|评级文件"
        job = self.progress.job(name, job_key)
        out: list[dict[str, Any]] = []
        if job.get("list_done"):
            self.log(f"  chinabond 评级文件 列表已完成 total={job.get('list_total')} listed={job.get('listed')}")
            return self._pending_from_index(name, "chinabond", "评级文件")
        start_page = int(job.get("next_page") or 1)
        self.log(f"  chinabond 评级文件 从第 {start_page} 页 ...")
        last_total = 0
        for pack in self.cb.iter_pages(name, start_page=start_page, max_pages=self.max_pages):
            self.wait_if_paused()
            added = 0
            for item in pack["items"]:
                row = self._to_row(seq, name, item)
                if item.get("locked"):
                    row["status"] = "locked"
                    row["error"] = "skip_login"
                else:
                    row["status"] = "listed" if item.get("pdf_url") else "no_file"
                key = _row_key(row)
                prev = self._index.get(key) if key else None
                if prev and prev.get("status") in {"ok", "listed", "locked", "no_file"}:
                    out.append({**item, "_row": prev})
                    continue
                self._write_row(row)
                added += 1
                out.append({**item, "_row": row})
                if item.get("locked"):
                    self.progress.add_download(name, job_key, "locked")
            last_total = pack["total"]
            self.progress.mark_page(
                name,
                job_key,
                page=pack["page"],
                total=pack["total"],
                pages=pack["pages"],
                added=added,
            )
            self.log(
                f"    页 {pack['page']}/{pack['pages']} 本页新 {added} 条，接口总数 {pack['total']}"
            )
        self.progress.mark_list_done(name, job_key, last_total)
        return out

    def _pending_from_index(self, name: str, source: str, category: str) -> list[dict[str, Any]]:
        out = []
        with self._jsonl_lock:
            snapshot = list(self._index.values())
        for row in snapshot:
            if row.get("issuer_name") != name:
                continue
            if row.get("source") != source or row.get("category") != category:
                continue
            if not _row_relevant(name, row):
                continue
            item = {
                "source": source,
                "category": category,
                "issuer_name": name,
                "title": row.get("title"),
                "agency": row.get("agency"),
                "publish_date": row.get("publish_date"),
                "content_id": row.get("content_id"),
                "doc_id": row.get("doc_id"),
                "suffix": "pdf",
                "detail_url": row.get("detail_url"),
                "pdf_url": row.get("pdf_url"),
                "locked": row.get("status") == "locked",
                "_row": row,
            }
            out.append(item)
        return out

    def _download_many(self, name: str, items: list[dict[str, Any]]) -> None:
        todo = []
        already = 0
        for item in items:
            row = item.get("_row") or {}
            if not _row_relevant(name, row if row.get("title") else item):
                continue
            if item.get("locked") or row.get("status") == "locked":
                continue
            if not item.get("pdf_url"):
                continue
            if row.get("status") == "ok" and row.get("local_path"):
                dest = self.root / row["local_path"]
                if dest.exists() and dest.stat().st_size > 1000:
                    already += 1
                    continue
            dest = self._dest_path(row if row.get("issuer_name") else self._to_row(0, name, item), item)
            if dest.exists() and dest.stat().st_size > 1000:
                row["local_path"] = str(dest.relative_to(self.root))
                row["file_size"] = dest.stat().st_size
                row["sha256"] = sha256_file(dest)
                row["status"] = "ok"
                self._write_row(row)
                self.progress.add_download(name, _job_key(item), "download_skip")
                already += 1
                continue
            todo.append(item)
        self._hook("listed", {"issuer": name, "total": already + len(todo), "done": already})
        if not todo:
            self.log("  下载无可新文件")
            return
        self.log(f"  下载 {len(todo)} 个文件 workers={self.workers}")
        with ThreadPoolExecutor(max_workers=self.workers) as pool:
            futs = {pool.submit(self._download_one, name, item): item for item in todo}
            for fut in as_completed(futs):
                item = futs[fut]
                try:
                    status = fut.result()
                except Exception as e:
                    status = "fail"
                    self.log(f"    [fail] {item.get('title')}: {e}")
                self.log(f"    [{status}] {item.get('source')} {item.get('publish_date')} {str(item.get('title') or '')[:50]}")

    def _download_one(self, name: str, item: dict[str, Any]) -> str:
        self.wait_if_paused()
        row = dict(item.get("_row") or self._to_row(0, name, item))
        dest = self._dest_path(row, item)
        job_key = _job_key(item)
        task_id = str(item.get("content_id") or id(item))
        self._hook(
            "file_start",
            {
                "id": task_id,
                "title": item.get("title") or "",
                "source": item.get("source") or "",
                "issuer": name,
            },
        )
        try:
            client = self.cm if item.get("source") == "chinamoney" else self.cb
            data, remote_name = client.download(item)
            ext = guess_ext(item.get("suffix") or Path(str(remote_name)).suffix, data)
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
            self._write_row(row)
            self.progress.add_download(name, job_key, "download_ok" if row["status"] == "ok" else "download_fail")
            self._hook(
                "file_done",
                {
                    "id": task_id,
                    "status": row["status"],
                    "path": row.get("local_path") or "",
                    "issuer": name,
                },
            )
            return row["status"]
        except Exception as e:
            row["status"] = "fail"
            row["error"] = f"{type(e).__name__}: {e}"
            self._write_row(row)
            self.progress.add_download(name, job_key, "download_fail")
            self._hook("file_done", {"id": task_id, "status": "fail", "path": "", "issuer": name})
            return "fail"

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
            "query_start": self.cb.start_date if item.get("source") == "chinabond" else self.cm.start_date,
            "query_end": self.cb.end_date if item.get("source") == "chinabond" else self.cm.end_date,
            "local_path": "",
            "file_size": 0,
            "sha256": "",
            "status": "",
            "error": "",
            "is_duplicate": "0",
            "duplicate_of": "",
            "dup_reason": "",
        }

    def _dest_path(self, row: dict[str, Any], item: dict[str, Any]) -> Path:
        issuer_dir = self.download_dir / (row.get("source") or item.get("source") or "misc") / issuer_dirname(
            row.get("issuer_name") or item.get("issuer_name") or "unknown"
        )
        date_s = str(row.get("publish_date") or "undated")[:10].replace("-", "")
        agency = sanitize_filename(row.get("agency") or "未知机构", max_len=20)
        title = sanitize_filename(row.get("title") or item.get("content_id") or "report", max_len=70)
        cid = sanitize_filename(str(item.get("content_id") or ""), max_len=24)
        ext = (item.get("suffix") or "pdf").lstrip(".") or "pdf"
        return issuer_dir / f"{date_s}_{agency}_{title}_{cid}.{ext}"

    def _flush_tables(self) -> None:
        with self._flush_lock:
            self._flush_tables_unlocked()

    def _flush_tables_unlocked(self) -> None:
        rows = load_jsonl(self.records_path)
        rows = _drop_false_hits(rows)
        rows = _last_wins(rows)
        mark_duplicates(rows)
        write_tables(
            rows,
            self.output_dir,
            query_start=self.cm.start_date,
            query_end=self.cm.end_date,
        )


def _row_relevant(issuer: str, row: dict[str, Any]) -> bool:
    title = str(row.get("title") or "")
    cat = str(row.get("category") or "")
    if not title:
        return True
    if cat == "债项评级报告":
        return True
    return issuer in title


def _row_key(row: dict[str, Any]) -> str:
    cid = str(row.get("content_id") or "")
    if not cid:
        return ""
    return f"{row.get('source')}|{cid}|{row.get('issuer_name')}"


def _job_key(item: dict[str, Any]) -> str:
    return f"{item.get('source')}|{item.get('category')}"


def _drop_false_hits(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    kept = []
    for row in rows:
        issuer = row.get("issuer_name") or ""
        title = row.get("title") or ""
        cat = row.get("category") or ""
        if row.get("status") == "empty" or not title:
            kept.append(row)
            continue
        if issuer in title or cat == "债项评级报告":
            kept.append(row)
    return kept


def _last_wins(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    indexed: dict[str, dict[str, Any]] = {}
    extras: list[dict[str, Any]] = []
    for row in rows:
        key = _row_key(row)
        if not key:
            extras.append(row)
            continue
        indexed[key] = row
    return extras + list(indexed.values())

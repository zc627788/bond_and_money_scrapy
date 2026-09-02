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
            stats = {src: self._source_disk_stats(name, src) for src in sources}
            lists_ok = self._lists_complete(name, sources)
            need = (not lists_ok) or any(st["need"] for st in stats.values())
            if resume and lists_ok and not need:
                bits = []
                for src in sources:
                    st = stats[src]
                    label = "货币网" if src == "chinamoney" else "债券网"
                    if st["ok"]:
                        bits.append(f"{label} {st['ok']} 个文件")
                    else:
                        bits.append(f"{label} 无文件")
                    if st["locked"]:
                        bits[-1] += f"，锁定 {st['locked']}"
                self.log(f"[skip] {seq} {name} already done，{'；'.join(bits)}")
                self._hook("skipped", {"seq": seq, "name": name, "by_source": stats})
                continue
            if resume and lists_ok and need:
                self._log_repair(seq, name, stats)
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
                    err = f"{type(e).__name__}: {e}"
                    self.log(f"  chinamoney {cat.get('label')} 列表失败，稍后断点续: {err}")
                    self._hook(
                        "list_error",
                        {
                            "issuer": name,
                            "source": "chinamoney",
                            "category": str(cat.get("label") or ""),
                            "error": err,
                        },
                    )
        if "chinabond" in sources:
            try:
                pending.extend(self._list_chinabond(seq, name))
            except Exception as e:
                err = f"{type(e).__name__}: {e}"
                self.log(f"  chinabond 列表失败，稍后断点续: {err}")
                self._hook(
                    "list_error",
                    {
                        "issuer": name,
                        "source": "chinabond",
                        "category": "评级文件",
                        "error": err,
                    },
                )

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
            for src in sources:
                if self._source_list_complete(name, src):
                    self._hook(
                        "listed",
                        {
                            "issuer": name,
                            "source": src,
                            "total": 0,
                            "done": 0,
                            "skip": 0,
                            "locked": 0,
                            "missing": 0,
                            "retry": 0,
                        },
                    )
                self._emit_source_done(name, src)
            return self._issuer_complete(name, sources)

        if download:
            for src in sources:
                subset = [it for it in pending if it.get("source") == src]
                self._download_many(name, subset, source=src)
                self._emit_source_done(name, src)
        else:
            for src in sources:
                self._emit_source_done(name, src)
        return self._issuer_complete(name, sources)

    def _issuer_file_counts(self, name: str) -> dict[str, int]:
        jobs = (self.progress.snapshot(name).get("jobs") or {}).values()
        return {
            "ok": sum(int(j.get("download_ok") or 0) for j in jobs),
            "fail": sum(int(j.get("download_fail") or 0) for j in jobs),
            "skip": sum(int(j.get("download_skip") or 0) for j in jobs),
            "locked": sum(int(j.get("locked") or 0) for j in jobs),
        }

    def _log_repair(self, seq: int, name: str, stats: dict[str, dict[str, int]]) -> None:
        bits = []
        for src, st in stats.items():
            label = "货币网" if src == "chinamoney" else "债券网"
            parts = []
            if st["missing"]:
                parts.append(f"缺失 {st['missing']}")
            if st["fail"]:
                parts.append(f"失败 {st['fail']}")
            if parts:
                bits.append(f"{label} {'、'.join(parts)}")
        self.log(f"[repair] {seq} {name} 将重下：{'；'.join(bits) or '未完成列表'}")

    def _file_exists(self, row: dict[str, Any]) -> bool:
        path = row.get("local_path") or ""
        if not path:
            return False
        dest = self.root / path
        try:
            return dest.exists() and dest.stat().st_size > 1000
        except OSError:
            return False

    def _source_disk_stats(self, name: str, source: str) -> dict[str, int]:
        ok = fail = locked = missing = 0
        with self._jsonl_lock:
            snapshot = list(self._index.values())
        for row in snapshot:
            if row.get("issuer_name") != name or row.get("source") != source:
                continue
            if not _row_relevant(name, row):
                continue
            st = row.get("status")
            if st == "locked":
                locked += 1
            elif st in {"fail", "not_pdf"}:
                fail += 1
            elif st == "ok":
                if self._file_exists(row):
                    ok += 1
                else:
                    missing += 1
        return {
            "ok": ok,
            "fail": fail,
            "locked": locked,
            "missing": missing,
            "need": int(fail + missing > 0),
        }

    def _issuer_complete(self, name: str, sources: tuple[str, ...]) -> bool:
        if not self._lists_complete(name, sources):
            return False
        return not any(self._source_disk_stats(name, src)["need"] for src in sources)

    def _download_reason(self, name: str, item: dict[str, Any]) -> str:
        row = item.get("_row") or {}
        if item.get("locked") or row.get("status") == "locked":
            return "locked"
        if not item.get("pdf_url"):
            return "no_url"
        if self._file_exists(row):
            return "exists"
        dest = self._dest_path(row if row.get("issuer_name") else self._to_row(0, name, item), item)
        if dest.exists() and dest.stat().st_size > 1000:
            return "exists_path"
        if row.get("status") == "ok":
            return "missing"
        if row.get("status") in {"fail", "not_pdf"}:
            return "retry_fail"
        return "new"

    def _source_list_keys(self, source: str) -> list[str]:
        if source == "chinamoney":
            return [f"chinamoney|{cat.get('label')}" for cat in self.cm_categories]
        if source == "chinabond":
            return ["chinabond|评级文件"]
        return []

    def _source_list_complete(self, name: str, source: str) -> bool:
        jobs = self.progress.snapshot(name).get("jobs") or {}
        keys = self._source_list_keys(source)
        return bool(keys) and all((jobs.get(k) or {}).get("list_done") for k in keys)

    def _lists_complete(self, name: str, sources: tuple[str, ...]) -> bool:
        return all(self._source_list_complete(name, src) for src in sources)

    def _emit_source_done(self, name: str, source: str) -> None:
        list_ok = self._source_list_complete(name, source)
        stats = self._source_disk_stats(name, source)
        if not list_ok:
            phase = "failed"
        elif stats["need"]:
            phase = "failed"
        elif stats["ok"] or stats["locked"]:
            phase = "done"
        else:
            phase = "empty"
        self._hook(
            "source_done",
            {
                "issuer": name,
                "source": source,
                "phase": phase,
                "ok": stats["ok"],
                "fail": stats["fail"],
                "locked": stats["locked"],
            },
        )

    def _brief_items(self, items: list[dict[str, Any]], *, page: int, category: str) -> list[dict[str, Any]]:
        out = []
        for it in items:
            status = "locked" if it.get("locked") else "listed"
            if not it.get("pdf_url") and status != "locked":
                status = "no_file"
            out.append(
                {
                    "id": str(it.get("content_id") or ""),
                    "title": str(it.get("title") or "")[:100],
                    "category": category or str(it.get("category") or ""),
                    "page": page,
                    "status": status,
                }
            )
        return out

    def _list_chinamoney(self, seq: int, name: str, cat: dict[str, Any]) -> list[dict[str, Any]]:
        label = str(cat.get("label") or "债项评级报告")
        job_key = f"chinamoney|{label}"
        job = self.progress.job(name, job_key)
        out: list[dict[str, Any]] = []
        if job.get("list_done"):
            self.log(f"  chinamoney {label} 列表已完成 total={job.get('list_total')} listed={job.get('listed')}")
            cached = self._pending_from_index(name, "chinamoney", label)
            pages = int(job.get("list_pages") or 0)
            self._hook(
                "list_done",
                {
                    "issuer": name,
                    "source": "chinamoney",
                    "category": label,
                    "cached": True,
                    "page": pages,
                    "pages": pages,
                    "total": int(job.get("list_total") or 0),
                    "items": self._brief_items(cached, page=pages, category=label),
                },
            )
            return cached
        start_page = int(job.get("next_page") or 1)
        self.log(f"  chinamoney {label} 从第 {start_page} 页 ...")
        self._hook(
            "list_start",
            {"issuer": name, "source": "chinamoney", "category": label, "page": start_page, "pages": 0},
        )
        last_total = 0
        last_page = start_page
        last_pages = 0
        http = self._bind_progress("chinamoney", issuer=name, category=label, scope="list")
        try:
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
                last_page = pack["page"]
                last_pages = pack["pages"]
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
                for it in pack["items"]:
                    it["_page"] = pack["page"]
                self._hook(
                    "list_page",
                    {
                        "issuer": name,
                        "source": "chinamoney",
                        "category": label,
                        "page": pack["page"],
                        "pages": pack["pages"],
                        "total": pack["total"],
                        "added": added,
                        "items": self._brief_items(pack["items"], page=pack["page"], category=label),
                    },
                )
            self.progress.mark_list_done(name, job_key, last_total)
            self._hook(
                "list_done",
                {
                    "issuer": name,
                    "source": "chinamoney",
                    "category": label,
                    "page": last_page,
                    "pages": last_pages,
                    "total": last_total,
                },
            )
            return out
        finally:
            http.bind_progress(None)

    def _list_chinabond(self, seq: int, name: str) -> list[dict[str, Any]]:
        job_key = "chinabond|评级文件"
        job = self.progress.job(name, job_key)
        out: list[dict[str, Any]] = []
        if job.get("list_done"):
            self.log(f"  chinabond 评级文件 列表已完成 total={job.get('list_total')} listed={job.get('listed')}")
            cached = self._pending_from_index(name, "chinabond", "评级文件")
            pages = int(job.get("list_pages") or 0)
            self._hook(
                "list_done",
                {
                    "issuer": name,
                    "source": "chinabond",
                    "category": "评级文件",
                    "cached": True,
                    "page": pages,
                    "pages": pages,
                    "total": int(job.get("list_total") or 0),
                    "items": self._brief_items(cached, page=pages, category="评级文件"),
                },
            )
            return cached
        start_page = int(job.get("next_page") or 1)
        self.log(f"  chinabond 评级文件 从第 {start_page} 页 ...")
        self._hook(
            "list_start",
            {"issuer": name, "source": "chinabond", "category": "评级文件", "page": start_page, "pages": 0},
        )
        last_total = 0
        last_page = start_page
        last_pages = 0
        http = self._bind_progress("chinabond", issuer=name, category="评级文件", scope="list")
        try:
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
                last_page = pack["page"]
                last_pages = pack["pages"]
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
                for it in pack["items"]:
                    it["_page"] = pack["page"]
                self._hook(
                    "list_page",
                    {
                        "issuer": name,
                        "source": "chinabond",
                        "category": "评级文件",
                        "page": pack["page"],
                        "pages": pack["pages"],
                        "total": pack["total"],
                        "added": added,
                        "items": self._brief_items(pack["items"], page=pack["page"], category="评级文件"),
                    },
                )
            self.progress.mark_list_done(name, job_key, last_total)
            self._hook(
                "list_done",
                {
                    "issuer": name,
                    "source": "chinabond",
                    "category": "评级文件",
                    "page": last_page,
                    "pages": last_pages,
                    "total": last_total,
                },
            )
            return out
        finally:
            http.bind_progress(None)

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

    def _download_many(self, name: str, items: list[dict[str, Any]], source: str = "") -> None:
        todo: list[dict[str, Any]] = []
        already = 0
        locked_n = 0
        missing_n = 0
        retry_n = 0
        src = source or (items[0].get("source") if items else "")
        label = "货币网" if src == "chinamoney" else "债券网"
        for item in items:
            row = item.get("_row") or {}
            if not _row_relevant(name, row if row.get("title") else item):
                continue
            reason = self._download_reason(name, item)
            if reason == "locked":
                locked_n += 1
                self._hook_file(name, item, status="locked", error="需登录，已跳过")
                continue
            if reason == "no_url":
                self._hook_file(name, item, status="no_file", error="无附件")
                continue
            if reason == "exists":
                already += 1
                self._hook_file(name, item, status="skip", error="本地已有")
                continue
            if reason == "exists_path":
                dest = self._dest_path(row if row.get("issuer_name") else self._to_row(0, name, item), item)
                row["local_path"] = str(dest.relative_to(self.root))
                row["file_size"] = dest.stat().st_size
                row["sha256"] = sha256_file(dest)
                row["status"] = "ok"
                row["error"] = ""
                self._write_row(row)
                self.progress.add_download(name, _job_key(item), "download_skip")
                already += 1
                self._hook_file(name, item, status="skip", error="本地已有")
                continue
            if reason == "missing":
                missing_n += 1
            elif reason == "retry_fail":
                retry_n += 1
            todo.append(item)
        self._hook(
            "listed",
            {
                "issuer": name,
                "source": src,
                "total": already + len(todo),
                "done": already,
                "skip": already,
                "locked": locked_n,
                "missing": missing_n,
                "retry": retry_n,
            },
        )
        if not todo:
            extra = f"，锁定 {locked_n}" if locked_n else ""
            self.log(f"  {label} 无可新文件{extra}" if items or locked_n else f"  {label} 无记录")
            return
        bits = [f"{len(todo)} 个"]
        if missing_n:
            bits.append(f"缺失 {missing_n}")
        if retry_n:
            bits.append(f"失败重试 {retry_n}")
        if already:
            bits.append(f"已有 {already}")
        if locked_n:
            bits.append(f"锁定跳过 {locked_n}")
        self.log(f"  {label} 下载 {'，'.join(bits)} workers={self.workers}")
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
        prev = str(row.get("status") or "")
        source = str(item.get("source") or "")
        self._hook(
            "file_start",
            {
                "id": task_id,
                "title": item.get("title") or "",
                "source": source,
                "issuer": name,
                "category": item.get("category") or "",
                "page": int(item.get("_page") or 0),
                "status": "downloading",
            },
        )
        http = self._bind_progress(
            source,
            issuer=name,
            id=task_id,
            title=item.get("title") or "",
            category=item.get("category") or "",
            page=int(item.get("_page") or 0),
            scope="download",
        )
        try:
            client = self.cm if source == "chinamoney" else self.cb
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
            else:
                row["error"] = ""
            self._write_row(row)
            self._bump_download_counter(name, job_key, prev, row["status"])
            self._hook(
                "file_done",
                {
                    "id": task_id,
                    "status": row["status"],
                    "path": row.get("local_path") or "",
                    "issuer": name,
                    "source": source,
                    "title": item.get("title") or "",
                    "category": item.get("category") or "",
                    "page": int(item.get("_page") or 0),
                    "error": row.get("error") or "",
                },
            )
            return row["status"]
        except Exception as e:
            login = "login" in str(e).lower() or "locked" in str(e).lower()
            row["status"] = "locked" if login else "fail"
            row["error"] = "skip_login" if login else f"{type(e).__name__}: {e}"
            self._write_row(row)
            self._bump_download_counter(name, job_key, prev, row["status"])
            self._hook(
                "file_done",
                {
                    "id": task_id,
                    "status": row["status"],
                    "path": "",
                    "issuer": name,
                    "source": source,
                    "title": item.get("title") or "",
                    "category": item.get("category") or "",
                    "page": int(item.get("_page") or 0),
                    "error": row.get("error") or "",
                },
            )
            return row["status"]
        finally:
            http.bind_progress(None)

    def _bind_progress(self, source: str, **ctx: Any):
        http = self.http_cm if source == "chinamoney" else self.http_cb

        def _cb(payload: dict[str, Any]) -> None:
            self._hook("attempt", {"source": source, **ctx, **payload})

        http.bind_progress(_cb)
        return http

    def _hook_file(self, name: str, item: dict[str, Any], *, status: str, error: str = "") -> None:
        self._hook(
            "file_done",
            {
                "id": str(item.get("content_id") or ""),
                "status": status,
                "path": "",
                "issuer": name,
                "source": item.get("source") or "",
                "title": item.get("title") or "",
                "category": item.get("category") or "",
                "page": int(item.get("_page") or 0),
                "error": error,
            },
        )

    def _bump_download_counter(self, name: str, job_key: str, prev: str, now: str) -> None:
        if prev == now:
            if now == "ok":
                return
        if prev == "fail":
            self.progress.add_download(name, job_key, "download_fail", -1)
        elif prev == "locked":
            self.progress.add_download(name, job_key, "locked", -1)
        if now == "ok":
            if prev != "ok":
                self.progress.add_download(name, job_key, "download_ok")
        elif now == "locked":
            self.progress.add_download(name, job_key, "locked")
        else:
            self.progress.add_download(name, job_key, "download_fail")

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

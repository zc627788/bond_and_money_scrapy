# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


class Progress:
    """按发行人 / 任务 / 页断点。兼容旧 state.json 的 done 名单。"""

    def __init__(self, path: Path, legacy_state: Path | None = None):
        self.path = path
        self._lock = threading.Lock()
        self.data: dict[str, Any] = {"issuers": {}}
        if path.exists():
            try:
                raw = path.read_text(encoding="utf-8-sig")
                self.data = json.loads(raw) if raw.strip() else {"issuers": {}}
            except json.JSONDecodeError:
                try:
                    self.data, _ = json.JSONDecoder().raw_decode(raw)
                except Exception:
                    print(f"  [progress] 断点文件损坏，已忽略: {path}", flush=True)
                    self.data = {"issuers": {}}
            except Exception:
                self.data = {"issuers": {}}
        if "issuers" not in self.data:
            self.data["issuers"] = {}
        if legacy_state and legacy_state.exists():
            self._import_legacy(legacy_state)

    def _import_legacy(self, legacy_state: Path) -> None:
        try:
            old = json.loads(legacy_state.read_text(encoding="utf-8"))
        except Exception:
            return
        for name in old.get("done") or []:
            rec = self.data["issuers"].get(name)
            if rec and rec.get("jobs"):
                continue
            rec = self.data["issuers"].setdefault(name, {})
            rec["status"] = "done"
            rec.setdefault("jobs", {})

    def is_done(self, issuer: str) -> bool:
        with self._lock:
            return self.data.get("issuers", {}).get(issuer, {}).get("status") == "done"

    def issuer(self, name: str, seq: int) -> dict[str, Any]:
        with self._lock:
            rec = self.data["issuers"].setdefault(name, {"seq": seq, "status": "running", "jobs": {}})
            rec.setdefault("jobs", {})
            rec["seq"] = seq
            return rec

    def job(self, issuer: str, job_key: str) -> dict[str, Any]:
        rec = self.data["issuers"].setdefault(issuer, {"status": "running", "jobs": {}})
        jobs = rec.setdefault("jobs", {})
        return jobs.setdefault(
            job_key,
            {
                "list_done": False,
                "list_total": 0,
                "list_pages": 0,
                "next_page": 1,
                "listed": 0,
                "download_ok": 0,
                "download_fail": 0,
                "download_skip": 0,
                "locked": 0,
            },
        )

    def mark_page(self, issuer: str, job_key: str, *, page: int, total: int, pages: int, added: int) -> None:
        with self._lock:
            job = self.job(issuer, job_key)
            job["list_total"] = total
            job["list_pages"] = pages
            job["next_page"] = page + 1
            job["listed"] = int(job.get("listed") or 0) + added
            if pages and page >= pages:
                job["list_done"] = True
            self._save_unlocked()

    def mark_list_done(self, issuer: str, job_key: str, total: int) -> None:
        with self._lock:
            job = self.job(issuer, job_key)
            job["list_done"] = True
            job["list_total"] = total
            self._save_unlocked()

    def add_download(self, issuer: str, job_key: str, field: str, n: int = 1) -> None:
        with self._lock:
            job = self.job(issuer, job_key)
            job[field] = max(0, int(job.get(field) or 0) + n)
            self._save_unlocked()

    def mark_issuer(self, issuer: str, status: str) -> None:
        with self._lock:
            rec = self.data["issuers"].setdefault(issuer, {"jobs": {}})
            rec["status"] = status
            rec["updated_at"] = datetime.now(timezone.utc).isoformat()
            self._save_unlocked()

    def snapshot(self, issuer: str) -> dict[str, Any]:
        return dict(self.data.get("issuers", {}).get(issuer) or {})

    def _save_unlocked(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

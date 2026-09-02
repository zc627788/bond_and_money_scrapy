# -*- coding: utf-8 -*-
"""极简桌面界面：CSV 与手填并集、按公司进度、暂停续跑。"""
from __future__ import annotations

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .names import load_issuer_csv, parse_manual_names
from .pipeline import Crawler
from .util import app_root, bundled_root, load_json

DEFAULT_PROXY = (
    "http://58ip.top/api/get?token=9e5d4546c8092014354afd46897b1e"
    "&number=50&type=http&format=1"
)
TEMPLATE_CSV = "issuer_name\n七台河市城市建设投资发展有限公司\n万正投资集团有限公司\n"
MAX_CONCUR = 50


def _clamp(n: int, lo: int = 1, hi: int = MAX_CONCUR) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return lo
    return max(lo, min(hi, v))


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("评级报告抓取")
        self.geometry("820x620")
        self.minsize(720, 520)
        self.root_dir = app_root()
        self.bundle_dir = bundled_root()
        self.cfg_path = self.bundle_dir / "config" / "settings.json"
        self.settings = {}
        if self.cfg_path.exists():
            self.settings = load_json(self.cfg_path)
        self._crawler: Crawler | None = None
        self._worker: threading.Thread | None = None
        self._q: queue.Queue = queue.Queue()
        self._co: dict[str, dict] = {}
        self._build()
        self.after(120, self._pump)

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 4}

        top = ttk.Frame(self)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="公司名单").pack(side="left")
        ttk.Button(top, text="下载模板", command=self._save_template).pack(side="left", padx=(12, 4))
        ttk.Button(top, text="导入 CSV", command=self._pick_csv).pack(side="left")
        self.count_label = ttk.Label(top, text="共 0 家", foreground="#555")
        self.count_label.pack(side="left", padx=12)

        self.manual = tk.Text(self, height=7, wrap="word", undo=True)
        self.manual.pack(fill="x", padx=12)
        self.manual.bind("<KeyRelease>", lambda _e: self._refresh_count())
        ttk.Label(
            self,
            text="可直接粘贴或导入 CSV（并集去重）。一行一个，或英文逗号分隔。",
            foreground="#888",
        ).pack(anchor="w", padx=12)

        opt = ttk.Frame(self)
        opt.pack(fill="x", **pad)
        ttk.Label(opt, text="同时抓几家").pack(side="left")
        self.issuer_workers = tk.IntVar(value=_clamp(self.settings.get("issuer_workers", 4)))
        ttk.Spinbox(opt, from_=1, to=MAX_CONCUR, width=5, textvariable=self.issuer_workers).pack(side="left", padx=4)
        ttk.Label(opt, text="同时下载几个").pack(side="left", padx=(16, 0))
        self.workers = tk.IntVar(value=_clamp(self.settings.get("workers", 8)))
        ttk.Spinbox(opt, from_=1, to=MAX_CONCUR, width=5, textvariable=self.workers).pack(side="left", padx=4)
        ttk.Label(opt, text="上限 50", foreground="#888").pack(side="left", padx=8)

        proxy_fr = ttk.Frame(self)
        proxy_fr.pack(fill="x", **pad)
        ttk.Label(proxy_fr, text="代理").pack(side="left")
        self.proxy = tk.StringVar(value=DEFAULT_PROXY)
        ttk.Entry(proxy_fr, textvariable=self.proxy).pack(side="left", fill="x", expand=True, padx=6)

        btn = ttk.Frame(self)
        btn.pack(fill="x", **pad)
        self.btn_start = ttk.Button(btn, text="开始", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_pause = ttk.Button(btn, text="暂停", command=self.pause, state="disabled")
        self.btn_pause.pack(side="left", padx=6)
        self.btn_resume = ttk.Button(btn, text="继续", command=self.resume, state="disabled")
        self.btn_resume.pack(side="left")
        ttk.Button(btn, text="打开结果", command=self._open_out).pack(side="right")
        self.status = ttk.Label(btn, text="就绪")
        self.status.pack(side="left", padx=16)

        ttk.Label(self, text="按公司进度").pack(anchor="w", padx=12, pady=(8, 0))
        cols = ("name", "progress", "current")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=12)
        self.tree.heading("name", text="公司")
        self.tree.heading("progress", text="进度")
        self.tree.heading("current", text="当前文件")
        self.tree.column("name", width=240, stretch=False)
        self.tree.column("progress", width=110, stretch=False)
        self.tree.column("current", width=420)
        self.tree.pack(fill="both", expand=True, padx=12, pady=6)
        self._refresh_count()

    def _names_in_box(self) -> list[str]:
        return [n for _, n in parse_manual_names(self.manual.get("1.0", "end"))]

    def _write_names(self, names: list[str]) -> None:
        self.manual.delete("1.0", "end")
        self.manual.insert("1.0", "\n".join(names))
        self._refresh_count()

    def _refresh_count(self) -> None:
        n = len(self._names_in_box())
        self.count_label.config(text=f"共 {n} 家")

    def _save_template(self) -> None:
        path = filedialog.asksaveasfilename(
            title="保存名单模板",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv")],
            initialfile="issuers_template.csv",
        )
        if not path:
            return
        bundled = self.bundle_dir / "templates" / "issuers_template.csv"
        text = bundled.read_text(encoding="utf-8") if bundled.exists() else TEMPLATE_CSV
        Path(path).write_text(text, encoding="utf-8-sig")
        messagebox.showinfo("模板", "已保存。填公司全称后点「导入 CSV」，会合并进上方名单。")

    def _pick_csv(self) -> None:
        path = filedialog.askopenfilename(title="导入名单 CSV", filetypes=[("CSV", "*.csv"), ("全部", "*.*")])
        if not path:
            return
        try:
            imported = [n for _, n in load_issuer_csv(Path(path))]
        except Exception as e:
            messagebox.showerror("导入失败", str(e))
            return
        if not imported:
            messagebox.showerror("导入失败", "CSV 里没有公司名称")
            return
        old = self._names_in_box()
        seen = set(old)
        added = 0
        for n in imported:
            if n not in seen:
                old.append(n)
                seen.add(n)
                added += 1
        self._write_names(old)
        messagebox.showinfo("导入完成", f"读入 {len(imported)} 家，新增 {added} 家，名单共 {len(old)} 家。")

    def _issuers(self) -> list[tuple[int, str]]:
        names = self._names_in_box()
        return [(i + 1, n) for i, n in enumerate(names)]

    def _settings(self) -> dict:
        settings = dict(self.settings) if self.settings else {}
        settings["workers"] = _clamp(self.workers.get())
        settings["issuer_workers"] = _clamp(self.issuer_workers.get())
        self.workers.set(settings["workers"])
        self.issuer_workers.set(settings["issuer_workers"])
        settings["max_pages"] = 0
        api = (self.proxy.get() or "").strip()
        settings["proxy"] = {
            "enabled": bool(api),
            "api": api,
            "max_extract": 50,
            "refresh_seconds": 180,
        }
        settings.setdefault("chinamoney", {})["start_date"] = "1990-01-01"
        settings.setdefault("chinabond", {})["start_date"] = "1990-01-01"
        settings["download_dir"] = "downloads"
        settings["output_dir"] = "output"
        return settings

    def start(self) -> None:
        issuers = self._issuers()
        if not issuers:
            messagebox.showerror("名单", "请在文本框填写公司，或先导入 CSV")
            return
        self._co.clear()
        for iid in self.tree.get_children():
            self.tree.delete(iid)
        self.btn_start.config(state="disabled")
        self.btn_pause.config(state="normal")
        self.btn_resume.config(state="disabled")
        self.status.config(text=f"抓取中 · {len(issuers)} 家")
        hooks = {
            "log": lambda m: self._q.put(("log", m)),
            "issuer": lambda p: self._q.put(("issuer", p)),
            "listed": lambda p: self._q.put(("listed", p)),
            "file_start": lambda p: self._q.put(("file_start", p)),
            "file_done": lambda p: self._q.put(("file_done", p)),
        }
        crawler = Crawler(self._settings(), self.root_dir, hooks=hooks)
        self._crawler = crawler

        def work() -> None:
            try:
                crawler.run(issuers, sources=("chinamoney", "chinabond"), download=True, resume=True)
                self._q.put(("done", "完成"))
            except Exception as e:
                self._q.put(("done", f"失败: {e}"))

        self._worker = threading.Thread(target=work, daemon=True)
        self._worker.start()

    def pause(self) -> None:
        if self._crawler:
            self._crawler.pause()
            self.btn_pause.config(state="disabled")
            self.btn_resume.config(state="normal")
            self.status.config(text="已暂停")

    def resume(self) -> None:
        if self._crawler:
            self._crawler.resume()
            self.btn_pause.config(state="normal")
            self.btn_resume.config(state="disabled")
            self.status.config(text="抓取中")

    def _ensure_company(self, name: str) -> dict:
        if name not in self._co:
            iid = self.tree.insert("", "end", values=(name, "等待", ""))
            self._co[name] = {"iid": iid, "total": 0, "done": 0, "fail": 0, "current": ""}
        return self._co[name]

    def _paint(self, name: str) -> None:
        st = self._co.get(name)
        if not st:
            return
        total = int(st["total"])
        done = int(st["done"])
        fail = int(st["fail"])
        finished = done + fail
        if total <= 0:
            prog = "无文件"
        else:
            pct = int(finished * 100 / total)
            prog = f"{finished}/{total}  {pct}%"
        self.tree.item(st["iid"], values=(name, prog, st.get("current") or ""))

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "issuer":
                    self._ensure_company(payload.get("name") or "")
                    self._paint(payload.get("name") or "")
                elif kind == "listed":
                    name = payload.get("issuer") or ""
                    st = self._ensure_company(name)
                    st["total"] = int(payload.get("total") or 0)
                    st["done"] = int(payload.get("done") or 0)
                    self._paint(name)
                elif kind == "file_start":
                    name = payload.get("issuer") or ""
                    st = self._ensure_company(name)
                    src = "货币网" if payload.get("source") == "chinamoney" else "中债"
                    title = str(payload.get("title") or "")[:60]
                    st["current"] = f"{src} · {title}"
                    self._paint(name)
                elif kind == "file_done":
                    name = payload.get("issuer") or ""
                    st = self._ensure_company(name)
                    if payload.get("status") == "ok":
                        st["done"] = int(st["done"]) + 1
                    else:
                        st["fail"] = int(st["fail"]) + 1
                    if st["current"] and "失败" not in str(st["current"]):
                        pass
                    self._paint(name)
                elif kind == "done":
                    self.status.config(text=str(payload))
                    self.btn_start.config(state="normal")
                    self.btn_pause.config(state="disabled")
                    self.btn_resume.config(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._pump)

    def _open_out(self) -> None:
        path = self.root_dir / "output"
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # noqa: S606  windows


def main() -> None:
    App().mainloop()

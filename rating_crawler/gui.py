# -*- coding: utf-8 -*-
"""极简桌面界面：导入名单 / 手动输入 / 暂停续跑 / 下载列表。"""
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


class App(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("评级报告抓取")
        self.geometry("760x560")
        self.minsize(680, 480)
        self.root_dir = app_root()
        self.bundle_dir = bundled_root()
        self.cfg_path = self.bundle_dir / "config" / "settings.json"
        self.settings = {}
        if self.cfg_path.exists():
            self.settings = load_json(self.cfg_path)
        self._crawler: Crawler | None = None
        self._worker: threading.Thread | None = None
        self._q: queue.Queue = queue.Queue()
        self._rows: dict[str, str] = {}
        self._build()
        self.after(120, self._pump)

    def _build(self) -> None:
        pad = {"padx": 12, "pady": 4}
        mode_fr = ttk.Frame(self)
        mode_fr.pack(fill="x", **pad)
        ttk.Label(mode_fr, text="名单").pack(side="left")
        self.mode = tk.StringVar(value="csv")
        ttk.Radiobutton(mode_fr, text="导入 CSV", variable=self.mode, value="csv", command=self._toggle).pack(side="left", padx=8)
        ttk.Radiobutton(mode_fr, text="手动输入", variable=self.mode, value="manual", command=self._toggle).pack(side="left")

        csv_fr = ttk.Frame(self)
        csv_fr.pack(fill="x", **pad)
        ttk.Button(csv_fr, text="下载模板", command=self._save_template).pack(side="left")
        ttk.Button(csv_fr, text="选择文件", command=self._pick_csv).pack(side="left", padx=6)
        self.csv_label = ttk.Label(csv_fr, text="未选择", foreground="#666")
        self.csv_label.pack(side="left")
        self.csv_path: Path | None = None

        self.manual = tk.Text(self, height=3, wrap="word")
        self.manual.insert("1.0", "临泉县交通建设投资有限责任公司,万正投资集团有限公司")
        self.manual.pack(fill="x", padx=12)
        ttk.Label(self, text="手动输入用英文逗号分隔公司全称", foreground="#888").pack(anchor="w", padx=12)

        opt = ttk.Frame(self)
        opt.pack(fill="x", **pad)
        ttk.Label(opt, text="同时抓几家").pack(side="left")
        self.issuer_workers = tk.IntVar(value=int(self.settings.get("issuer_workers", 4)))
        ttk.Spinbox(opt, from_=1, to=10, width=4, textvariable=self.issuer_workers).pack(side="left", padx=4)
        ttk.Label(opt, text="同时下载几个").pack(side="left", padx=(16, 0))
        self.workers = tk.IntVar(value=int(self.settings.get("workers", 8)))
        ttk.Spinbox(opt, from_=1, to=32, width=4, textvariable=self.workers).pack(side="left", padx=4)

        proxy_fr = ttk.Frame(self)
        proxy_fr.pack(fill="x", **pad)
        ttk.Label(proxy_fr, text="代理").pack(side="left")
        self.proxy = tk.StringVar(value=DEFAULT_PROXY)
        ttk.Entry(proxy_fr, textvariable=self.proxy).pack(side="left", fill="x", expand=True, padx=6)
        ttk.Label(self, text="留空则直连；默认查全部（双源、1990 至今）", foreground="#888").pack(anchor="w", padx=12)

        btn = ttk.Frame(self)
        btn.pack(fill="x", **pad)
        self.btn_start = ttk.Button(btn, text="开始", command=self.start)
        self.btn_start.pack(side="left")
        self.btn_pause = ttk.Button(btn, text="暂停", command=self.pause, state="disabled")
        self.btn_pause.pack(side="left", padx=6)
        self.btn_resume = ttk.Button(btn, text="继续", command=self.resume, state="disabled")
        self.btn_resume.pack(side="left")
        ttk.Button(btn, text="查看明细", command=self._show_log).pack(side="right")
        ttk.Button(btn, text="打开结果", command=self._open_out).pack(side="right", padx=6)
        self.status = ttk.Label(btn, text="就绪")
        self.status.pack(side="left", padx=16)

        ttk.Label(self, text="正在下载").pack(anchor="w", padx=12, pady=(8, 0))
        cols = ("title", "source", "status")
        self.tree = ttk.Treeview(self, columns=cols, show="headings", height=12)
        self.tree.heading("title", text="文件")
        self.tree.heading("source", text="来源")
        self.tree.heading("status", text="状态")
        self.tree.column("title", width=460)
        self.tree.column("source", width=100)
        self.tree.column("status", width=80)
        self.tree.pack(fill="both", expand=True, padx=12, pady=6)

        self.log_win: tk.Toplevel | None = None
        self.log_text: tk.Text | None = None
        self._toggle()

    def _toggle(self) -> None:
        if self.mode.get() == "manual":
            self.manual.configure(state="normal", background="#fff")
        else:
            self.manual.configure(state="normal")

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
        messagebox.showinfo("模板", "已保存，填公司全称后用「选择文件」导入。")

    def _pick_csv(self) -> None:
        path = filedialog.askopenfilename(title="选择名单 CSV", filetypes=[("CSV", "*.csv"), ("全部", "*.*")])
        if not path:
            return
        self.csv_path = Path(path)
        self.csv_label.config(text=self.csv_path.name)
        self.mode.set("csv")

    def _issuers(self) -> list[tuple[int, str]]:
        if self.mode.get() == "manual":
            return parse_manual_names(self.manual.get("1.0", "end"))
        if not self.csv_path:
            raise ValueError("请先选择 CSV，或切换到手动输入")
        return load_issuer_csv(self.csv_path)

    def _settings(self) -> dict:
        settings = dict(self.settings) if self.settings else {}
        settings["workers"] = int(self.workers.get())
        settings["issuer_workers"] = int(self.issuer_workers.get())
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
        try:
            issuers = self._issuers()
        except Exception as e:
            messagebox.showerror("名单", str(e))
            return
        if not issuers:
            messagebox.showerror("名单", "没有公司名称")
            return
        self.btn_start.config(state="disabled")
        self.btn_pause.config(state="normal")
        self.btn_resume.config(state="disabled")
        self.status.config(text=f"抓取中 · {len(issuers)} 家")
        hooks = {
            "log": lambda m: self._q.put(("log", m)),
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

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "log":
                    self._append_log(str(payload))
                elif kind == "file_start":
                    self._row_upsert(payload["id"], payload.get("title") or "", payload.get("source") or "", "下载中")
                elif kind == "file_done":
                    st = "完成" if payload.get("status") == "ok" else "失败"
                    self._row_status(payload["id"], st)
                elif kind == "done":
                    self.status.config(text=str(payload))
                    self.btn_start.config(state="normal")
                    self.btn_pause.config(state="disabled")
                    self.btn_resume.config(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._pump)

    def _row_upsert(self, tid: str, title: str, source: str, status: str) -> None:
        src = "货币网" if source == "chinamoney" else "中债" if source == "chinabond" else source
        if tid in self._rows:
            iid = self._rows[tid]
            self.tree.item(iid, values=(title[:80], src, status))
            return
        iid = self.tree.insert("", 0, values=(title[:80], src, status))
        self._rows[tid] = iid
        kids = self.tree.get_children()
        if len(kids) > 80:
            self.tree.delete(kids[-1])

    def _row_status(self, tid: str, status: str) -> None:
        iid = self._rows.get(tid)
        if not iid:
            return
        vals = self.tree.item(iid, "values")
        if vals:
            self.tree.item(iid, values=(vals[0], vals[1], status))

    def _append_log(self, msg: str) -> None:
        if not self.log_text:
            return
        self.log_text.insert("end", msg + "\n")
        self.log_text.see("end")

    def _show_log(self) -> None:
        if self.log_win and self.log_win.winfo_exists():
            self.log_win.lift()
            return
        win = tk.Toplevel(self)
        win.title("抓取明细")
        win.geometry("720x420")
        text = tk.Text(win, wrap="word")
        text.pack(fill="both", expand=True)
        self.log_win = win
        self.log_text = text

    def _open_out(self) -> None:
        path = self.root_dir / "output"
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # noqa: S606  windows


def main() -> None:
    App().mainloop()

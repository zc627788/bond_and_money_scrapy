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
from .util import app_root, bundled_root, load_json, today_str

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
        self.geometry("1180x740")
        self.minsize(980, 640)
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
        self._trees: dict[str, ttk.Treeview] = {}
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

        self._build_fixed_params()

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

        panes = ttk.Frame(self)
        panes.pack(fill="both", expand=True, padx=12, pady=6)
        panes.columnconfigure(0, weight=1)
        panes.columnconfigure(1, weight=1)
        panes.rowconfigure(0, weight=1)
        self._trees["chinamoney"] = self._make_tree(panes, 0, "中国货币网")
        self._trees["chinabond"] = self._make_tree(panes, 1, "中国债券信息网")
        self._refresh_count()

    def _make_tree(self, parent: ttk.Frame, col: int, title: str) -> ttk.Treeview:
        box = ttk.LabelFrame(parent, text=title)
        box.grid(row=0, column=col, sticky="nsew", padx=(0, 8) if col == 0 else (8, 0))
        cols = ("name", "progress", "current")
        tree = ttk.Treeview(box, columns=cols, show="headings", height=12)
        tree.heading("name", text="公司")
        tree.heading("progress", text="进度")
        tree.heading("current", text="当前 / 说明")
        tree.column("name", width=180, stretch=False)
        tree.column("progress", width=150, stretch=False)
        tree.column("current", width=220)
        tree.tag_configure("skipped", foreground="#666")
        tree.tag_configure("queued", foreground="#888")
        tree.tag_configure("fail", foreground="#a40000")
        vsb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vsb.pack(side="right", fill="y", pady=6)
        return tree

    def _build_fixed_params(self) -> None:
        box = ttk.LabelFrame(self, text="本次查询（固定，不可改）")
        box.pack(fill="x", padx=12, pady=(4, 2))
        box.columnconfigure(1, weight=1)
        box.columnconfigure(3, weight=1)

        cm = self.settings.get("chinamoney") or {}
        cb = self.settings.get("chinabond") or {}
        start = str(cm.get("start_date") or cb.get("start_date") or "1990-01-01")
        cats = [str(c.get("label") or "") for c in (cm.get("categories") or []) if c.get("label")]
        if not cats:
            cats = ["债项评级报告", "主体评级报告", "重点关注"]
        bond_tag = str(cb.get("child_chnl_desc") or "评级文件")
        if bond_tag and bond_tag not in cats:
            cats.append(bond_tag)

        self._fixed_vars: dict[str, tk.StringVar] = {}

        def _field(r: int, c: int, label: str, value: str, span: int = 1) -> None:
            ttk.Label(box, text=label).grid(row=r, column=c * 2, sticky="e", padx=(8, 4), pady=3)
            var = tk.StringVar(value=value)
            ent = ttk.Entry(box, textvariable=var, state="disabled")
            ent.grid(row=r, column=c * 2 + 1, columnspan=span, sticky="ew", padx=(0, 8), pady=3)
            self._fixed_vars[label] = var

        _field(0, 0, "时间范围", f"{start} 至 {today_str()}（全部历史）")
        _field(0, 1, "数据来源", "中国货币网、中国债券信息网")
        _field(1, 0, "栏目标签", "、".join(cats), span=3)
        _field(2, 0, "断点续跑", "开启；失败和本地缺失会重下，成功文件跳过")
        _field(2, 1, "需登录文件", "直接失败（非公开发行企业债不重试）")
        _field(3, 0, "下载超时", "10 秒；不通则换 2 次代理，再直连，每次都是 10 秒", span=3)

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
        for tree in self._trees.values():
            for iid in tree.get_children():
                tree.delete(iid)
        for _, name in issuers:
            self._ensure_company(name)
            self._paint(name)
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
            "skipped": lambda p: self._q.put(("skipped", p)),
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

    def _blank_side(self) -> dict:
        return {
            "iid": "",
            "total": 0,
            "done": 0,
            "fail": 0,
            "skip": 0,
            "locked": 0,
            "missing": 0,
            "retry": 0,
            "current": "",
            "phase": "queued",
        }

    def _ensure_company(self, name: str) -> dict:
        if name not in self._co:
            rec: dict = {}
            for src, tree in self._trees.items():
                iid = tree.insert("", "end", values=(name, "排队", ""), tags=("queued",))
                rec[src] = {**self._blank_side(), "iid": iid}
            self._co[name] = rec
        return self._co[name]

    def _side(self, name: str, source: str) -> dict:
        rec = self._ensure_company(name)
        if source not in rec:
            rec[source] = self._blank_side()
        return rec[source]

    def _paint(self, name: str, source: str | None = None) -> None:
        rec = self._co.get(name)
        if not rec:
            return
        sources = [source] if source else list(self._trees)
        for src in sources:
            self._paint_side(name, src, rec.get(src) or self._blank_side())

    def _paint_side(self, name: str, source: str, st: dict) -> None:
        tree = self._trees.get(source)
        if not tree or not st.get("iid"):
            return
        total = int(st["total"])
        done = int(st["done"])
        fail = int(st["fail"])
        skip = int(st.get("skip") or 0)
        locked = int(st.get("locked") or 0)
        missing = int(st.get("missing") or 0)
        retry = int(st.get("retry") or 0)
        finished = done + fail
        phase = st.get("phase") or "queued"
        current = st.get("current") or ""
        tags: tuple[str, ...] = ()
        if phase == "skipped":
            prog = f"已爬取，跳过  {done}个文件" if done else "已爬取，跳过"
            current = current or "此前已完成，本次跳过"
            if locked:
                current += f" · 锁定 {locked}"
            tags = ("skipped",)
        elif phase == "queued":
            prog = "排队"
            tags = ("queued",)
        elif phase == "failed":
            if total <= 0:
                prog = "未完成"
            else:
                pct = int(finished * 100 / total) if total else 0
                prog = f"{finished}/{total}  {pct}%"
            current = current or "未全部完成，下次续跑"
            tags = ("fail",)
        elif total <= 0:
            prog = "查询中" if phase == "running" else "无文件"
            if locked and phase != "running":
                current = current or f"锁定 {locked}，已跳过"
        else:
            pct = int(finished * 100 / total)
            prog = f"{finished}/{total}  {pct}%"
            notes = []
            if skip and phase == "running" and done == skip and not fail:
                notes.append(f"已有 {skip}")
            if missing:
                notes.append(f"补缺失 {missing}")
            if retry:
                notes.append(f"重试失败 {retry}")
            if locked:
                notes.append(f"锁定 {locked}")
            if notes and (not current or current == "正在查询列表…"):
                current = " · ".join(notes)
            if phase == "done" and fail:
                prog += f" · 失败{fail}"
                tags = ("fail",)
            elif phase == "done" and finished >= total and not current:
                current = "完成"
        tree.item(st["iid"], values=(name, prog, current), tags=tags)

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "issuer":
                    name = payload.get("name") or ""
                    rec = self._ensure_company(name)
                    phase = payload.get("phase") or "start"
                    for src, st in rec.items():
                        if phase == "start":
                            st["phase"] = "running"
                            if not st.get("current"):
                                st["current"] = "正在查询列表…"
                        elif phase == "done":
                            if st.get("phase") != "skipped":
                                st["phase"] = "failed" if st.get("fail") else "done"
                                if st.get("current") in {"", "正在查询列表…"}:
                                    st["current"] = "完成" if st["phase"] == "done" else "未全部完成，下次续跑"
                        elif phase == "failed":
                            st["phase"] = "failed"
                    self._paint(name)
                elif kind == "skipped":
                    name = payload.get("name") or ""
                    rec = self._ensure_company(name)
                    by_source = payload.get("by_source") or {}
                    for src, st in rec.items():
                        info = by_source.get(src) or {}
                        st["phase"] = "skipped"
                        st["done"] = int(info.get("ok") or 0)
                        st["fail"] = 0
                        st["locked"] = int(info.get("locked") or 0)
                        st["skip"] = st["done"]
                        if st["done"]:
                            st["current"] = f"此前已完成，本次跳过 · {st['done']} 个文件"
                        else:
                            st["current"] = "此前已完成（无文件），本次跳过"
                        if st["locked"]:
                            st["current"] += f" · 锁定 {st['locked']}"
                    self._paint(name)
                elif kind == "listed":
                    name = payload.get("issuer") or ""
                    src = payload.get("source") or ""
                    if src not in self._trees:
                        continue
                    st = self._side(name, src)
                    st["phase"] = "running"
                    st["total"] = int(payload.get("total") or 0)
                    st["done"] = int(payload.get("done") or 0)
                    st["skip"] = int(payload.get("skip") or st["done"])
                    st["locked"] = int(payload.get("locked") or 0)
                    st["missing"] = int(payload.get("missing") or 0)
                    st["retry"] = int(payload.get("retry") or 0)
                    bits = []
                    if st["missing"]:
                        bits.append(f"补缺失 {st['missing']}")
                    if st["retry"]:
                        bits.append(f"重试失败 {st['retry']}")
                    if st["skip"]:
                        bits.append(f"已有 {st['skip']}")
                    if st["locked"]:
                        bits.append(f"锁定 {st['locked']}")
                    st["current"] = " · ".join(bits) if bits else st.get("current") or ""
                    self._paint(name, src)
                elif kind == "file_start":
                    name = payload.get("issuer") or ""
                    src = payload.get("source") or ""
                    if src not in self._trees:
                        continue
                    st = self._side(name, src)
                    st["current"] = str(payload.get("title") or "")[:50]
                    self._paint(name, src)
                elif kind == "file_done":
                    name = payload.get("issuer") or ""
                    src = payload.get("source") or ""
                    if src not in self._trees:
                        continue
                    st = self._side(name, src)
                    status = payload.get("status")
                    if status == "ok":
                        st["done"] = int(st["done"]) + 1
                    elif status == "locked":
                        st["locked"] = int(st.get("locked") or 0) + 1
                    else:
                        st["fail"] = int(st["fail"]) + 1
                    self._paint(name, src)
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

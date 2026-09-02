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
FILE_STATUS = {
    "listed": "待下载",
    "downloading": "下载中",
    "proxy": "走代理",
    "direct": "走直连",
    "retry": "重试中",
    "ok": "成功",
    "fail": "失败",
    "not_pdf": "失败",
    "locked": "需登录",
    "skip": "已有，跳过",
    "no_file": "无附件",
}


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
        self.geometry("1280x860")
        self.minsize(1040, 720)
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
        self._files: dict[tuple[str, str], dict[str, dict]] = {}
        self._sel: tuple[str, str] | None = None
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

        self.manual = tk.Text(self, height=4, wrap="word", undo=True)
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

        split = ttk.Panedwindow(self, orient="vertical")
        split.pack(fill="both", expand=True, padx=12, pady=(4, 8))
        panes = ttk.Frame(split)
        panes.columnconfigure(0, weight=1)
        panes.columnconfigure(1, weight=1)
        panes.rowconfigure(0, weight=1)
        self._trees["chinamoney"] = self._make_tree(panes, 0, "中国货币网")
        self._trees["chinabond"] = self._make_tree(panes, 1, "中国债券信息网")
        split.add(panes, weight=3)
        split.add(self._make_detail(split), weight=2)
        self._refresh_count()

    def _make_tree(self, parent: ttk.Frame, col: int, title: str) -> ttk.Treeview:
        box = ttk.LabelFrame(parent, text=title)
        box.grid(row=0, column=col, sticky="nsew", padx=(0, 8) if col == 0 else (8, 0))
        cols = ("name", "progress", "current", "detail")
        tree = ttk.Treeview(box, columns=cols, show="headings", height=10)
        tree.heading("name", text="公司")
        tree.heading("progress", text="进度")
        tree.heading("current", text="当前")
        tree.heading("detail", text="详情")
        tree.column("name", width=170, stretch=False)
        tree.column("progress", width=130, stretch=False)
        tree.column("current", width=180, stretch=False)
        tree.column("detail", width=200)
        tree.tag_configure("skipped", foreground="#666")
        tree.tag_configure("queued", foreground="#888")
        tree.tag_configure("fail", foreground="#a40000")
        vsb = ttk.Scrollbar(box, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        vsb.pack(side="right", fill="y", pady=6)
        src = "chinamoney" if "货币" in title else "chinabond"
        tree.bind("<<TreeviewSelect>>", lambda _e, s=src, t=tree: self._on_select(s, t))
        return tree

    def _make_detail(self, parent: ttk.Panedwindow) -> ttk.LabelFrame:
        box = ttk.LabelFrame(parent, text="详情（点选上方一行：页码、待下载、成功/失败原因）")
        head = ttk.Frame(box)
        head.pack(fill="x", padx=8, pady=(6, 2))
        self.detail_meta = ttk.Label(head, text="尚未选择公司", foreground="#555")
        self.detail_meta.pack(side="left")
        ttk.Button(head, text="复制选中链接", command=self._copy_selected_url).pack(side="right")
        ttk.Button(head, text="复制全部失败链接", command=self._copy_failed_urls).pack(side="right", padx=6)
        cols = ("title", "category", "page", "status", "error", "url")
        wrap = ttk.Frame(box)
        wrap.pack(fill="both", expand=True, padx=6, pady=(0, 6))
        self.detail = ttk.Treeview(wrap, columns=cols, show="headings", height=7)
        self.detail.heading("title", text="文件 / 标题")
        self.detail.heading("category", text="栏目")
        self.detail.heading("page", text="页")
        self.detail.heading("status", text="状态")
        self.detail.heading("error", text="失败原因")
        self.detail.heading("url", text="链接")
        self.detail.column("title", width=240)
        self.detail.column("category", width=100, stretch=False)
        self.detail.column("page", width=40, stretch=False)
        self.detail.column("status", width=80, stretch=False)
        self.detail.column("error", width=180, stretch=False)
        self.detail.column("url", width=280)
        self.detail.tag_configure("fail", foreground="#a40000")
        self.detail.tag_configure("ok", foreground="#1a7f37")
        self.detail.tag_configure("skip", foreground="#666")
        self.detail.tag_configure("retry", foreground="#b36b00")
        vsb = ttk.Scrollbar(wrap, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=vsb.set)
        self.detail.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.detail.bind("<Control-c>", lambda _e: self._copy_selected_url())
        self.detail.bind("<Double-1>", lambda _e: self._copy_selected_url())
        self.detail.bind("<Button-3>", self._detail_menu)
        return box

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
        _field(3, 0, "下载超时", "10 秒；5 次不同代理再直连，每次 10 秒；同一 IP 同时只给一条请求", span=3)

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
        self._files.clear()
        self._sel = None
        self._clear_detail("尚未选择公司")
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
            "list_start": lambda p: self._q.put(("list_start", p)),
            "list_page": lambda p: self._q.put(("list_page", p)),
            "list_done": lambda p: self._q.put(("list_done", p)),
            "list_error": lambda p: self._q.put(("list_error", p)),
            "file_start": lambda p: self._q.put(("file_start", p)),
            "file_done": lambda p: self._q.put(("file_done", p)),
            "attempt": lambda p: self._q.put(("attempt", p)),
            "source_done": lambda p: self._q.put(("source_done", p)),
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
            "page": 0,
            "pages": 0,
            "added": 0,
            "category": "",
            "list_error": "",
            "current": "",
            "phase": "queued",
        }

    def _ensure_company(self, name: str) -> dict:
        if name not in self._co:
            rec: dict = {}
            for src, tree in self._trees.items():
                iid = tree.insert("", "end", values=(name, "排队", "", "点选查看页码与文件"), tags=("queued",))
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
        page = int(st.get("page") or 0)
        pages = int(st.get("pages") or 0)
        category = st.get("category") or ""
        list_error = st.get("list_error") or ""
        finished = done + fail
        phase = st.get("phase") or "queued"
        current = st.get("current") or ""
        detail = "点选查看页码与文件"
        tags: tuple[str, ...] = ()
        page_s = f"{page}/{pages}页" if pages else (f"第{page}页" if page else "")
        if phase == "skipped":
            prog = f"已爬取，跳过  {done}个文件" if done else "已爬取，跳过"
            current = current or "此前已完成，本次跳过"
            if locked:
                current += f" · 锁定 {locked}"
            detail = f"已有 {done} 个文件" if done else "无文件"
            tags = ("skipped",)
        elif phase in {"queued", "waiting"}:
            prog = "排队" if phase == "queued" else "等待查询"
            tags = ("queued",)
        elif phase == "listing":
            prog = f"列表 {page_s}" if page_s else "查询列表"
            current = current or (f"{category} · {page_s}" if category else "正在翻页")
            detail = f"{category} {page_s}".strip()
            if st.get("added"):
                detail += f" · 本页新 {st['added']} 条"
        elif phase == "failed":
            tags = ("fail",)
            if list_error:
                prog = f"列表失败 {page_s}".strip() if page_s else "列表失败"
                current = list_error[:60]
                detail = f"停在 {page_s or '列表'} · {list_error[:40]}"
            elif total <= 0:
                prog = "失败"
                current = current or "未查到列表，下次续跑"
            else:
                pct = int(finished * 100 / total) if total else 0
                prog = f"{finished}/{total}  {pct}%"
                if fail:
                    prog += f" · 失败{fail}"
                current = current or "未全部完成，下次续跑"
                detail = f"成功 {done} · 失败 {fail}"
        elif phase == "empty":
            prog = "无文件"
            current = current or "该源没有可下载报告"
            detail = "列表完成，0 个文件"
        elif total <= 0 and phase in {"running", "downloading"}:
            prog = "查询中" if phase == "running" else "准备下载"
            current = current or category or "正在查询列表"
            detail = f"{category} {page_s}".strip() or "点选查看"
        elif total > 0:
            pct = int(finished * 100 / total) if total else 0
            prog = f"{finished}/{total}  {pct}%"
            if fail:
                prog += f" · 失败{fail}"
            if phase == "downloading" and not current:
                current = "正在下载"
            notes = []
            if page_s:
                notes.append(f"列表 {page_s}")
            if skip:
                notes.append(f"已有 {skip}")
            if missing:
                notes.append(f"补缺失 {missing}")
            if retry:
                notes.append(f"重试 {retry}")
            if locked:
                notes.append(f"锁定 {locked}")
            if fail:
                notes.append(f"失败 {fail}")
            detail = " · ".join(notes) if notes else "点选查看文件"
            if phase == "done" and fail:
                tags = ("fail",)
            elif phase == "done" and not current:
                current = "完成"
        else:
            prog = "无文件" if phase == "done" else "查询中"
            current = current or ("完成" if phase == "done" else "正在查询列表")
        tree.item(st["iid"], values=(name, prog, current, detail), tags=tags)

    def _pump(self) -> None:
        try:
            while True:
                kind, payload = self._q.get_nowait()
                if kind == "issuer":
                    name = payload.get("name") or ""
                    rec = self._ensure_company(name)
                    phase = payload.get("phase") or "start"
                    if phase == "start":
                        for src, st in rec.items():
                            if st.get("phase") in {"queued", "waiting"}:
                                st["phase"] = "waiting"
                                st["current"] = "等待该源查询"
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
                elif kind == "list_start":
                    name = payload.get("issuer") or ""
                    src = payload.get("source") or ""
                    if src not in self._trees:
                        continue
                    st = self._side(name, src)
                    st["phase"] = "listing"
                    st["category"] = str(payload.get("category") or "")
                    st["page"] = int(payload.get("page") or 1)
                    st["pages"] = int(payload.get("pages") or 0)
                    st["list_error"] = ""
                    st["current"] = f"{st['category']} · 开始翻页"
                    self._paint(name, src)
                elif kind in {"list_page", "list_done"}:
                    name = payload.get("issuer") or ""
                    src = payload.get("source") or ""
                    if src not in self._trees:
                        continue
                    st = self._side(name, src)
                    if st.get("phase") not in {"failed", "skipped", "downloading", "done"}:
                        st["phase"] = "listing"
                    st["category"] = str(payload.get("category") or st.get("category") or "")
                    if payload.get("page"):
                        st["page"] = int(payload.get("page") or 0)
                    if payload.get("pages"):
                        st["pages"] = int(payload.get("pages") or 0)
                    st["added"] = int(payload.get("added") or 0)
                    if kind == "list_page":
                        st["current"] = f"{st['category']} · {st['page']}/{st['pages'] or '?'}页"
                    for it in payload.get("items") or []:
                        self._upsert_file(name, src, it)
                    self._paint(name, src)
                    self._maybe_refresh_detail(name, src)
                elif kind == "list_error":
                    name = payload.get("issuer") or ""
                    src = payload.get("source") or ""
                    if src not in self._trees:
                        continue
                    st = self._side(name, src)
                    st["phase"] = "failed"
                    st["category"] = str(payload.get("category") or st.get("category") or "")
                    st["list_error"] = str(payload.get("error") or "列表失败")
                    st["current"] = st["list_error"][:60]
                    self._paint(name, src)
                    self._maybe_refresh_detail(name, src)
                elif kind == "listed":
                    name = payload.get("issuer") or ""
                    src = payload.get("source") or ""
                    if src not in self._trees:
                        continue
                    st = self._side(name, src)
                    if st.get("phase") != "failed":
                        st["phase"] = "downloading"
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
                    if bits:
                        st["current"] = " · ".join(bits)
                    self._paint(name, src)
                elif kind == "attempt":
                    name = payload.get("issuer") or ""
                    src = payload.get("source") or ""
                    if src not in self._trees:
                        continue
                    st = self._side(name, src)
                    label = str(payload.get("label") or "")
                    title = str(payload.get("title") or "")
                    cat = str(payload.get("category") or st.get("category") or "")
                    if payload.get("scope") == "list" or not title:
                        page = int(payload.get("page") or st.get("page") or 0)
                        pages = int(st.get("pages") or 0)
                        prefix = f"{cat} {page}/{pages or '?'}页" if (cat or page) else "查询列表"
                        st["current"] = f"{prefix} · {label}" if label else prefix
                    else:
                        short = title[:28]
                        st["current"] = f"{short} · {label}" if label else short
                    if payload.get("id") or title:
                        self._upsert_file(
                            name,
                            src,
                            {
                                "id": payload.get("id") or "",
                                "title": title,
                                "category": cat,
                                "page": payload.get("page") or st.get("page") or "",
                                "status": payload.get("status") or "retry",
                                "error": label,
                            },
                        )
                    self._paint(name, src)
                    self._maybe_refresh_detail(name, src)
                elif kind == "file_start":
                    name = payload.get("issuer") or ""
                    src = payload.get("source") or ""
                    if src not in self._trees:
                        continue
                    st = self._side(name, src)
                    if st.get("phase") != "failed":
                        st["phase"] = "downloading"
                    st["current"] = str(payload.get("title") or "")[:50]
                    payload = {**payload, "status": "downloading"}
                    self._upsert_file(name, src, payload)
                    self._paint(name, src)
                    self._maybe_refresh_detail(name, src)
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
                    elif status in {"fail", "not_pdf"}:
                        st["fail"] = int(st["fail"]) + 1
                    self._upsert_file(name, src, payload)
                    self._paint(name, src)
                    self._maybe_refresh_detail(name, src)
                elif kind == "source_done":
                    name = payload.get("issuer") or ""
                    src = payload.get("source") or ""
                    if src not in self._trees:
                        continue
                    st = self._side(name, src)
                    phase = payload.get("phase") or "done"
                    if st.get("phase") != "failed" or phase == "failed":
                        st["phase"] = phase
                    if phase == "empty":
                        st["current"] = "该源没有可下载报告"
                    elif phase == "done" and not st.get("fail"):
                        if st.get("current") in {"", "等待该源查询", "正在查询列表…"}:
                            st["current"] = "完成"
                    elif phase == "failed" and not st.get("list_error"):
                        st["current"] = st.get("current") or "未全部完成，下次续跑"
                    self._paint(name, src)
                    self._maybe_refresh_detail(name, src)
                elif kind == "done":
                    self.status.config(text=str(payload))
                    self.btn_start.config(state="normal")
                    self.btn_pause.config(state="disabled")
                    self.btn_resume.config(state="disabled")
        except queue.Empty:
            pass
        self.after(120, self._pump)

    def _on_select(self, source: str, tree: ttk.Treeview) -> None:
        sel = tree.selection()
        if not sel:
            return
        vals = tree.item(sel[0], "values")
        if not vals:
            return
        name = str(vals[0])
        self._sel = (name, source)
        self._refresh_detail()

    def _upsert_file(self, name: str, source: str, payload: dict) -> None:
        fid = str(payload.get("id") or payload.get("title") or "")
        if not fid:
            return
        store = self._files.setdefault((name, source), {})
        rec = store.get(fid) or {}
        rec.update(
            {
                "id": fid,
                "title": payload.get("title") or rec.get("title") or fid,
                "category": payload.get("category") or rec.get("category") or "",
                "page": payload.get("page") if payload.get("page") not in (None, "") else rec.get("page") or "",
                "status": payload.get("status") or rec.get("status") or "listed",
                "error": payload.get("error") or rec.get("error") or "",
                "url": payload.get("url") or payload.get("pdf_url") or rec.get("url") or "",
            }
        )
        store[fid] = rec

    def _maybe_refresh_detail(self, name: str, source: str) -> None:
        if self._sel == (name, source):
            self._refresh_detail()

    def _clear_detail(self, msg: str) -> None:
        self.detail_meta.config(text=msg)
        for iid in self.detail.get_children():
            self.detail.delete(iid)

    def _refresh_detail(self) -> None:
        if not self._sel:
            self._clear_detail("尚未选择公司")
            return
        name, source = self._sel
        src_label = "中国货币网" if source == "chinamoney" else "中国债券信息网"
        st = (self._co.get(name) or {}).get(source) or {}
        files = list((self._files.get((name, source)) or {}).values())
        counts = {"listed": 0, "downloading": 0, "ok": 0, "fail": 0, "skip": 0, "locked": 0, "no_file": 0}
        for f in files:
            key = str(f.get("status") or "listed")
            if key == "not_pdf":
                key = "fail"
            counts[key] = counts.get(key, 0) + 1
        page = st.get("page") or 0
        pages = st.get("pages") or 0
        cat = st.get("category") or ""
        err = st.get("list_error") or ""
        bits = [f"{name} · {src_label}"]
        if cat:
            bits.append(cat)
        if pages or page:
            bits.append(f"列表 {page}/{pages or '?'} 页")
        bits.append(
            f"待下 {counts['listed']+counts['downloading']} · 成功 {counts['ok']} · 失败 {counts['fail']} · 跳过 {counts['skip']}"
        )
        if counts["locked"]:
            bits.append(f"锁定 {counts['locked']}")
        if err:
            bits.append(f"列表错误：{err[:80]}")
        self.detail_meta.config(text="  |  ".join(bits))
        for iid in self.detail.get_children():
            self.detail.delete(iid)
        order = {
            "downloading": 0,
            "proxy": 0,
            "direct": 0,
            "retry": 0,
            "fail": 1,
            "not_pdf": 1,
            "listed": 2,
            "ok": 3,
            "skip": 4,
            "locked": 5,
            "no_file": 6,
        }
        files.sort(key=lambda x: (order.get(str(x.get("status")), 9), str(x.get("page") or ""), str(x.get("title") or "")))
        for f in files[:400]:
            status = str(f.get("status") or "listed")
            tag = (
                "fail"
                if status in {"fail", "not_pdf"}
                else (
                    "ok"
                    if status == "ok"
                    else (
                        "retry"
                        if status in {"proxy", "direct", "retry", "downloading"}
                        else ("skip" if status in {"skip", "locked", "no_file"} else "")
                    )
                )
            )
            page_s = str(f.get("page") or "")
            self.detail.insert(
                "",
                "end",
                values=(
                    str(f.get("title") or "")[:90],
                    str(f.get("category") or ""),
                    page_s,
                    FILE_STATUS.get(status, status),
                    str(f.get("error") or "")[:120],
                    str(f.get("url") or ""),
                ),
                tags=(tag,) if tag else (),
            )

    def _copy_text(self, text: str, ok_msg: str) -> None:
        text = (text or "").strip()
        if not text:
            self.status.config(text="没有可复制的链接")
            return
        self.clipboard_clear()
        self.clipboard_append(text)
        self.status.config(text=ok_msg)

    def _selected_detail_url(self) -> str:
        sel = self.detail.selection()
        if not sel:
            return ""
        vals = self.detail.item(sel[0], "values")
        if not vals or len(vals) < 6:
            return ""
        return str(vals[5] or "").strip()

    def _copy_selected_url(self) -> None:
        url = self._selected_detail_url()
        self._copy_text(url, "已复制选中链接")

    def _copy_failed_urls(self) -> None:
        urls: list[str] = []
        seen: set[str] = set()
        for iid in self.detail.get_children():
            vals = self.detail.item(iid, "values")
            if not vals or len(vals) < 6:
                continue
            status = str(vals[3] or "")
            url = str(vals[5] or "").strip()
            if status == "失败" and url and url not in seen:
                seen.add(url)
                urls.append(url)
        self._copy_text("\n".join(urls), f"已复制 {len(urls)} 条失败链接")

    def _detail_menu(self, event) -> None:
        row = self.detail.identify_row(event.y)
        if row:
            self.detail.selection_set(row)
        menu = tk.Menu(self, tearoff=0)
        menu.add_command(label="复制选中链接", command=self._copy_selected_url)
        menu.add_command(label="复制全部失败链接", command=self._copy_failed_urls)
        menu.tk_popup(event.x_root, event.y_root)

    def _open_out(self) -> None:
        path = self.root_dir / "output"
        path.mkdir(parents=True, exist_ok=True)
        os.startfile(path)  # noqa: S606  windows


def main() -> None:
    App().mainloop()

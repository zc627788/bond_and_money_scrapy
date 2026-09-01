# -*- coding: utf-8 -*-
from __future__ import annotations

import argparse
from pathlib import Path

from .names import load_issuers
from .pipeline import Crawler
from .util import load_json


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="从中国货币网 / 中国债券信息网抓取发行人评级报告 PDF，并汇总文件清单。"
    )
    src = p.add_mutually_exclusive_group()
    src.add_argument("--issuer", help="单个发行人全称")
    src.add_argument("--excel", help="发行人名单 xlsx，需含 issuer_name_clean 列")
    p.add_argument("--seq", type=int, help="只跑 excel 中指定 issuer_seq")
    p.add_argument("--start", type=int, default=1, help="excel 起始 issuer_seq（含）")
    p.add_argument("--limit", type=int, default=0, help="最多处理多少家，0 表示不限制")
    p.add_argument(
        "--source",
        choices=["both", "chinamoney", "chinabond"],
        default="both",
        help="抓取来源，默认双源",
    )
    p.add_argument("--no-download", action="store_true", help="只拉列表，不下载 PDF")
    p.add_argument("--no-resume", action="store_true", help="忽略断点，重跑未完成任务")
    p.add_argument("--workers", type=int, default=0, help="每家公司下载线程数，默认读 config workers")
    p.add_argument("--issuer-workers", type=int, default=0, help="同时爬几家公司，默认读 config issuer_workers")
    p.add_argument("--max-pages", type=int, default=0, help="每个栏目最多翻几页，0 表示不限制")
    p.add_argument("--no-proxy", action="store_true", help="禁用动态代理")
    p.add_argument(
        "--config",
        default="config/settings.json",
        help="配置文件路径",
    )
    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    cfg_path = Path(args.config)
    if not cfg_path.is_absolute():
        cfg_path = root / cfg_path
    settings = load_json(cfg_path)

    excel_default = root / "A09_lgfv_issuer_name_only.xlsx"
    if args.issuer:
        name = args.issuer.strip()
        seq = 0
        excel = Path(args.excel) if args.excel else excel_default
        if excel.exists():
            for s, n in load_issuers(excel):
                if n == name:
                    seq = s
                    break
        issuers = [(seq, name)]
    else:
        excel = Path(args.excel) if args.excel else excel_default
        if not excel.exists():
            raise SystemExit(f"找不到名单文件: {excel}")
        all_issuers = load_issuers(excel)
        if args.seq:
            issuers = [x for x in all_issuers if x[0] == args.seq]
            if not issuers:
                raise SystemExit(f"名单中没有 issuer_seq={args.seq}")
        else:
            issuers = [x for x in all_issuers if x[0] >= args.start]
            if args.limit:
                issuers = issuers[: args.limit]
        print(f"名单 {excel} 本轮 {len(issuers)} 家")

    sources = ("chinamoney", "chinabond") if args.source == "both" else (args.source,)
    if args.workers:
        settings["workers"] = args.workers
    if args.issuer_workers:
        settings["issuer_workers"] = args.issuer_workers
    if args.no_proxy:
        settings.setdefault("proxy", {})["enabled"] = False
    if args.max_pages:
        settings["max_pages"] = args.max_pages
    crawler = Crawler(settings, root)
    crawler.run(
        issuers,
        sources=sources,
        download=not args.no_download,
        resume=not args.no_resume,
    )


if __name__ == "__main__":
    main()

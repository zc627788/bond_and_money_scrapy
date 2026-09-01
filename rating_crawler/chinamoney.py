# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any, Iterator, Optional
from urllib.parse import unquote

from .http import UA, BrowserSession
from .util import guess_agency, is_pdf, today_str

HOME = "https://www.chinamoney.com.cn/chinese/pjgg/"
APPLY = "https://www.chinamoney.com.cn/dqs/rest/cm-u-rbt/apply"
CHANNELS = "https://www.chinamoney.com.cn/chinese/cxsymb/index.html"
LIST_API = "https://www.chinamoney.com.cn/ags/ms/cm-u-notice-issue/ratingAnNotice"
RBT_KEY = "==AO3QVZSV0VzkWNYdjSwhjW"[::-1]
FILE_DOWN = (
    "https://www.chinamoney.com.cn/dqs/cm-s-notice-query/fileDownLoad.do"
    "?contentId={content_id}&priority=0&mode=save"
)
FALLBACK_CHANNELS = {
    "zxpjbg": "2564",
    "ztpjbg": "2565",
    "zdgz": "2566",
}


def _headers(accept: str) -> dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://www.chinamoney.com.cn",
        "Referer": HOME,
        "X-Requested-With": "XMLHttpRequest",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


class ChinaMoneyClient:
    def __init__(self, http: BrowserSession, page_size: int = 15, start_date: str = "1990-01-01"):
        self.http = http
        self.page_size = page_size
        self.start_date = start_date
        self.end_date = today_str()
        self.channels: dict[str, str] = dict(FALLBACK_CHANNELS)
        self._channels_loaded = False
        self._ch_lock = threading.Lock()

    def warmup(self, http: Optional[BrowserSession] = None) -> None:
        sess = http or self.http
        sess.get(
            HOME,
            headers=_headers("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            retry_403=False,
        )
        sess.post(
            APPLY,
            data={"key": RBT_KEY},
            headers=_headers("application/json, text/javascript, */*; q=0.01"),
            retry_403=False,
        )
        sess.mark_warm()

    def ensure(self) -> None:
        if not self.http.is_warm():
            self.warmup()
        if not self._channels_loaded:
            with self._ch_lock:
                if not self._channels_loaded:
                    self._load_channels()
                    self._channels_loaded = True

    def _load_channels(self) -> None:
        resp = self.http.post(
            CHANNELS,
            headers=_headers("text/plain, */*; q=0.01"),
            warmup=self.warmup,
        )
        text = resp.text or ""
        try:
            cut = text[: text.rfind(",")] + "]"
            arr = json.loads(cut)
            mapping = {item["path"]: str(item["id"]) for item in arr if "path" in item and "id" in item}
            for key in FALLBACK_CHANNELS:
                if key in mapping:
                    self.channels[key] = mapping[key]
        except Exception as e:
            print(f"  [chinamoney] channel map parse failed, using fallback: {e}", flush=True)

    def iter_pages(
        self,
        issuer: str,
        *,
        scnd: str,
        channel_path: str,
        label: str,
        start_page: int = 1,
        max_pages: int = 0,
    ) -> Iterator[dict[str, Any]]:
        self.ensure()
        channel_id = self.channels.get(channel_path, FALLBACK_CHANNELS.get(channel_path, ""))
        page = max(1, start_page)
        while True:
            payload = {
                "channelId": channel_id,
                "bondSrno": "",
                "drftClAngl": "11",
                "scnd": scnd,
                "ratingOrg": "",
                "bondNameCode": issuer,
                "pageNo": str(page),
                "pageSize": str(self.page_size),
                "startDate": self.start_date,
                "endDate": self.end_date,
                "limit": "0",
                "timeln": "0",
            }
            self.http.sleep()
            resp = self.http.post(
                f"{LIST_API}?_={int(time.time() * 1000)}",
                data=payload,
                headers=_headers("application/json, text/javascript, */*; q=0.01"),
                warmup=self.warmup,
            )
            if resp.status_code != 200 or not resp.content:
                raise RuntimeError(f"chinamoney list {resp.status_code} page={page}")
            body = resp.json()
            rows = body.get("records") or []
            data = body.get("data") or {}
            total = int(data.get("total") or len(rows) or 0)
            try:
                pages = max(1, int(data.get("pageTotalSize") or 1))
            except (TypeError, ValueError):
                pages = 1
            items = _records_to_items(issuer, _keep_relevant(issuer, rows, label), label)
            cap = max_pages if max_pages else pages
            yield {"page": page, "pages": pages, "total": total, "items": items}
            if not rows or page >= pages or (max_pages and page >= max_pages):
                break
            page += 1
            if page > 80:
                break

    def download(self, item: dict[str, Any]) -> tuple[bytes, str]:
        self.ensure()
        url = item["pdf_url"]
        resp = self.http.get(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": HOME,
            },
            warmup=self.warmup,
            timeout=90,
        )
        data = resp.content or b""
        filename = _filename_from_cd(resp.headers.get("content-disposition", "")) or ""
        if resp.status_code != 200 or not data:
            raise RuntimeError(f"download failed {resp.status_code} {url}")
        if not is_pdf(data) and (item.get("suffix") == "pdf"):
            raise RuntimeError(f"not a pdf ({resp.headers.get('content-type')}) {url}")
        return data, filename


def _records_to_items(issuer: str, records: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    out = []
    seen: set[str] = set()
    for rec in records:
        cid = str(rec.get("contentId") or "")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        title = rec.get("title") or ""
        prefix = rec.get("prefix") or ""
        out.append(
            {
                "source": "chinamoney",
                "category": label,
                "issuer_name": issuer,
                "title": title,
                "agency": guess_agency(title, prefix),
                "publish_date": rec.get("releaseDate") or "",
                "content_id": cid,
                "doc_id": cid,
                "suffix": (rec.get("suffix") or "").lower(),
                "detail_url": "https://www.chinamoney.com.cn" + rec["draftPath"]
                if rec.get("draftPath")
                else HOME,
                "pdf_url": FILE_DOWN.format(content_id=cid),
                "locked": False,
            }
        )
    return out


def _keep_relevant(issuer: str, records: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    if not records:
        return []
    hit = any(issuer in (r.get("title") or "") for r in records)
    if label == "债项评级报告":
        return records if hit else []
    if not hit:
        return []
    return [r for r in records if issuer in (r.get("title") or "")]


def _filename_from_cd(cd: str) -> str:
    if not cd:
        return ""
    m = re.search(r"filename\*=UTF-8''([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    m = re.search(r'filename="([^"]+)"', cd, re.I)
    if m:
        return unquote(m.group(1))
    m = re.search(r"filename=([^;]+)", cd, re.I)
    if m:
        return unquote(m.group(1).strip().strip('"'))
    return ""

# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import re
import time
from typing import Any, Optional
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

# Fallback if /chinese/cxsymb/index.html changes shape.
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
    def __init__(self, http: BrowserSession, page_size: int = 15, start_date: str = "2006-08-01"):
        self.http = http
        self.page_size = page_size
        self.start_date = start_date
        self.end_date = today_str()
        self.channels: dict[str, str] = dict(FALLBACK_CHANNELS)
        self._ready = False

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
        self._ready = True

    def ensure(self) -> None:
        if not self._ready:
            self.warmup()
            self._load_channels()

    def _load_channels(self) -> None:
        self.http.sleep(0.4)
        resp = self.http.post(
            CHANNELS,
            headers=_headers("text/plain, */*; q=0.01"),
            warmup=self.warmup,
        )
        text = resp.text or ""
        # Site JSON has a trailing comma before ']'.
        try:
            cut = text[: text.rfind(",")] + "]"
            arr = json.loads(cut)
            mapping = {item["path"]: str(item["id"]) for item in arr if "path" in item and "id" in item}
            for key in FALLBACK_CHANNELS:
                if key in mapping:
                    self.channels[key] = mapping[key]
        except Exception as e:
            print(f"  [chinamoney] channel map parse failed, using fallback: {e}")

    def search_category(
        self,
        issuer: str,
        *,
        scnd: str,
        channel_path: str,
        label: str,
    ) -> list[dict[str, Any]]:
        self.ensure()
        channel_id = self.channels.get(channel_path, FALLBACK_CHANNELS.get(channel_path, ""))
        # Default window (~3y) is what the CDN reliably serves.
        records = _keep_relevant(
            issuer,
            self._paged(issuer, scnd=scnd, channel_id=channel_id, start="", end=""),
            label,
        )
        older = _keep_relevant(
            issuer,
            self._paged(
                issuer,
                scnd=scnd,
                channel_id=channel_id,
                start=self.start_date,
                end=self.end_date,
                retry_403=False,
            ),
            label,
        )
        records = older + records
        out = []
        seen: set[str] = set()
        for rec in records:
            cid = str(rec.get("contentId") or "")
            if not cid or cid in seen:
                continue
            seen.add(cid)
            title = rec.get("title") or ""
            prefix = rec.get("prefix") or ""
            item = {
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
                "raw": rec,
            }
            out.append(item)
        return out

    def _paged(
        self,
        issuer: str,
        *,
        scnd: str,
        channel_id: str,
        start: str,
        end: str,
        retry_403: bool = True,
    ) -> list[dict[str, Any]]:
        all_rows: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            payload = {
                "channelId": channel_id,
                "bondSrno": "",
                "drftClAngl": "11",
                "scnd": scnd,
                "ratingOrg": "",
                "bondNameCode": issuer,
                "pageNo": str(page),
                "pageSize": str(self.page_size),
                "startDate": start,
                "endDate": end,
                "limit": "0",
                "timeln": "0",
            }
            self.http.sleep()
            resp = self.http.post(
                f"{LIST_API}?_={int(time.time() * 1000)}",
                data=payload,
                headers=_headers("application/json, text/javascript, */*; q=0.01"),
                warmup=self.warmup,
                retry_403=retry_403,
            )
            if resp.status_code != 200 or not resp.content:
                print(f"  [chinamoney] list fail status={resp.status_code} page={page} start={start!r}")
                break
            try:
                body = resp.json()
            except Exception:
                print(f"  [chinamoney] non-json page={page}: {resp.content[:120]!r}")
                break
            rows = body.get("records") or []
            data = body.get("data") or {}
            try:
                total_pages = max(1, int(data.get("pageTotalSize") or 1))
            except (TypeError, ValueError):
                total_pages = 1
            all_rows.extend(rows)
            if not rows:
                break
            page += 1
            if page > 80:
                print("  [chinamoney] page cap reached")
                break
        return all_rows

    def download(self, item: dict[str, Any]) -> tuple[bytes, str]:
        self.ensure()
        url = item["pdf_url"]
        self.http.sleep(0.7)
        resp = self.http.get(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "application/pdf,application/octet-stream,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": HOME,
            },
            warmup=self.warmup,
        )
        data = resp.content or b""
        filename = _filename_from_cd(resp.headers.get("content-disposition", "")) or ""
        if resp.status_code != 200 or not data:
            raise RuntimeError(f"download failed {resp.status_code} {url}")
        if not is_pdf(data) and (item.get("suffix") == "pdf"):
            raise RuntimeError(f"not a pdf ({resp.headers.get('content-type')}) {url}")
        return data, filename


def _keep_relevant(issuer: str, records: list[dict[str, Any]], label: str) -> list[dict[str, Any]]:
    """Drop the site's 'latest reports' fallback when keyword search missed."""
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

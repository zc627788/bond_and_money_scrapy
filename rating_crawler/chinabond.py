# -*- coding: utf-8 -*-
from __future__ import annotations

from typing import Any, Optional
from urllib.parse import urljoin

from .http import UA, BrowserSession
from .util import guess_agency, is_pdf, today_str

HOME = "https://www.chinabond.com.cn/xxpl/ywzc_fxyfxdh/fxyfxdh_zqzl/"
LIST_API = "https://www.chinabond.com.cn/cbiw/trs/getContentByConditions"


def _headers(accept: str, referer: str = HOME) -> dict[str, str]:
    return {
        "User-Agent": UA,
        "Accept": accept,
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Origin": "https://www.chinabond.com.cn",
        "Referer": referer,
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    }


class ChinaBondClient:
    def __init__(
        self,
        http: BrowserSession,
        *,
        page_size: int = 20,
        start_date: str = "2006-01-01",
        parent_chnl_name: str = "fxyfxdh_zqzl",
        child_chnl_desc: str = "评级文件",
        exclude_parent_chnl_names: Optional[list[str]] = None,
        jrzq_chnl_name: str = "",
    ):
        self.http = http
        self.page_size = page_size
        self.start_date = start_date
        self.end_date = today_str()
        self.parent_chnl_name = parent_chnl_name
        self.child_chnl_desc = child_chnl_desc
        self.exclude_parent_chnl_names = exclude_parent_chnl_names or ["zkzl_dfzxxpl", "zkzl_lsz"]
        self.jrzq_chnl_name = jrzq_chnl_name
        self._ready = False

    def warmup(self, http: Optional[BrowserSession] = None) -> None:
        sess = http or self.http
        sess.get(
            HOME,
            headers=_headers("text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"),
            retry_403=False,
        )
        self._ready = True

    def ensure(self) -> None:
        if not self._ready:
            self.warmup()

    def search(self, issuer: str) -> list[dict[str, Any]]:
        self.ensure()
        items: list[dict[str, Any]] = []
        page = 1
        total_pages = 1
        while page <= total_pages:
            payload = {
                "parentChnlName": self.parent_chnl_name,
                "excludeParentChnlNames": self.exclude_parent_chnl_names,
                "childChnlDesc": self.child_chnl_desc,
                "jrzqChnlName": self.jrzq_chnl_name,
                "hasAppendix": True,
                "siteName": "chinaBond",
                "pageSize": self.page_size,
                "pageNum": page,
                "queryParam": {
                    "keywords": issuer,
                    "startDate": self.start_date,
                    "endDate": self.end_date,
                    "reportType": "",
                    "reportYear": "",
                    "ratingAgency": "",
                },
            }
            self.http.sleep()
            resp = self.http.post(
                LIST_API,
                json=payload,
                headers={
                    **_headers("application/json, text/plain, */*"),
                    "Content-Type": "application/json",
                },
                warmup=self.warmup,
            )
            if resp.status_code != 200 or not resp.content:
                print(f"  [chinabond] list fail status={resp.status_code} page={page}")
                break
            try:
                body = resp.json()
            except Exception:
                print(f"  [chinabond] non-json page={page}: {resp.content[:120]!r}")
                break
            if not body.get("success"):
                print(f"  [chinabond] api error code={body.get('code')} msg={body.get('msg')}")
                break
            data = body.get("data") or {}
            rows = data.get("list") or []
            total = int(data.get("total") or 0)
            total_pages = max(1, (total + self.page_size - 1) // self.page_size)
            for row in rows:
                items.extend(self._row_to_items(issuer, row))
            if not rows:
                break
            page += 1
            if page > 80:
                print("  [chinabond] page cap reached")
                break
        return items

    def _row_to_items(self, issuer: str, row: dict[str, Any]) -> list[dict[str, Any]]:
        title = row.get("docTitle") or ""
        pub = row.get("docPubUrl") or ""
        date_s = (row.get("shengXiaoShiJian") or "")[:10]
        doc_id = str(row.get("docid") or row.get("originDocId") or "")
        locked = bool(row.get("quanXianMa"))
        appendices = _parse_appendix(row.get("appendixIds") or "")
        if not appendices:
            return [
                {
                    "source": "chinabond",
                    "category": "评级文件",
                    "issuer_name": issuer,
                    "title": title,
                    "agency": guess_agency(title),
                    "publish_date": date_s,
                    "content_id": doc_id,
                    "doc_id": doc_id,
                    "suffix": "",
                    "detail_url": pub,
                    "pdf_url": "",
                    "locked": locked,
                    "raw": row,
                }
            ]
        out = []
        for idx, (fid, file_name, display_name) in enumerate(appendices):
            pdf_url = ""
            if pub and file_name:
                pdf_url = urljoin(pub.rsplit("/", 1)[0] + "/", file_name)
            out.append(
                {
                    "source": "chinabond",
                    "category": "评级文件",
                    "issuer_name": issuer,
                    "title": display_name or title,
                    "agency": guess_agency(display_name or title),
                    "publish_date": date_s,
                    "content_id": f"{doc_id}:{fid}" if fid else doc_id,
                    "doc_id": doc_id,
                    "suffix": (file_name.rsplit(".", 1)[-1].lower() if "." in file_name else "pdf"),
                    "detail_url": pub,
                    "pdf_url": pdf_url,
                    "locked": locked,
                    "raw": row,
                    "appendix_index": idx,
                }
            )
        return out

    def download(self, item: dict[str, Any]) -> tuple[bytes, str]:
        self.ensure()
        url = item.get("pdf_url") or ""
        if not url:
            raise RuntimeError("no pdf url")
        if item.get("locked"):
            raise RuntimeError("locked (quanXianMa)")
        self.http.sleep(0.7)
        resp = self.http.get(
            url,
            headers={
                "User-Agent": UA,
                "Accept": "application/pdf,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": item.get("detail_url") or HOME,
            },
            warmup=self.warmup,
        )
        data = resp.content or b""
        if resp.status_code != 200 or not data:
            raise RuntimeError(f"download failed {resp.status_code} {url}")
        if item.get("suffix") == "pdf" and not is_pdf(data):
            raise RuntimeError(f"not a pdf ({resp.headers.get('content-type')}) {url}")
        return data, item.get("title") or ""


def _parse_appendix(raw: str) -> list[tuple[str, str, str]]:
    """appendixIds: id=filename.pdf=display.pdf * id2=..."""
    out: list[tuple[str, str, str]] = []
    if not raw:
        return out
    for chunk in raw.split("*"):
        parts = chunk.split("=")
        if len(parts) < 2:
            continue
        fid = parts[0].strip()
        file_name = parts[1].strip()
        display = parts[2].strip() if len(parts) > 2 else file_name
        if display.lower().endswith(".pdf"):
            display = display[:-4]
        out.append((fid, file_name, display))
    return out

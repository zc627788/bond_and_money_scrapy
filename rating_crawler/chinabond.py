# -*- coding: utf-8 -*-
from __future__ import annotations

import re
from typing import Any, Iterator, Optional
from urllib.parse import urljoin

from .http import UA, BrowserSession, _looks_like_login
from .util import guess_agency, is_pdf, today_str

HOME = "https://www.chinabond.com.cn/xxpl/ywzc_fxyfxdh/fxyfxdh_zqzl/"
LIST_API = "https://www.chinabond.com.cn/cbiw/trs/getContentByConditions"
# ERR_E_Y_CK_CBIW_0001：括号等符号会被接口拒掉，只留中文/字母/数字/空格。
_KW_KEEP = re.compile(r"[^\w\s]+", re.UNICODE)


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
        page_size: int = 50,
        start_date: str = "1990-01-01",
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
        sess.mark_warm()

    def ensure(self) -> None:
        if not self.http.is_warm():
            self.warmup()
        self._ready = True

    def iter_pages(self, issuer: str, start_page: int = 1, max_pages: int = 0) -> Iterator[dict[str, Any]]:
        self.ensure()
        page = max(1, start_page)
        while True:
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
                    "keywords": _search_keywords(issuer),
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
                raise RuntimeError(f"chinabond list {resp.status_code} page={page}")
            body = resp.json()
            if not body.get("success"):
                raise RuntimeError(f"chinabond api {body.get('code')} {body.get('msg')}")
            data = body.get("data") or {}
            rows = data.get("list") or []
            total = int(data.get("total") or 0)
            pages = max(1, (total + self.page_size - 1) // self.page_size) if total else 1
            items = []
            for row in rows:
                items.extend(self._row_to_items(issuer, row))
            yield {"page": page, "pages": pages, "total": total, "items": items}
            if not rows or page >= pages or (max_pages and page >= max_pages):
                break
            page += 1
            if page > 80:
                break

    def _row_to_items(self, issuer: str, row: dict[str, Any]) -> list[dict[str, Any]]:
        title = row.get("docTitle") or ""
        pub = row.get("docPubUrl") or ""
        date_s = (row.get("shengXiaoShiJian") or "")[:10]
        doc_id = str(row.get("docid") or row.get("originDocId") or "")
        locked = bool(row.get("quanXianMa"))
        appendices = _parse_appendix(row.get("appendixIds") or "")
        if locked:
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
                    "suffix": "pdf",
                    "detail_url": pub,
                    "pdf_url": "",
                    "locked": True,
                }
            ]
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
                    "locked": False,
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
                    "locked": False,
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
            raise RuntimeError("login required")
        resp = self.http.get_file(
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
        if _looks_like_login(resp):
            raise RuntimeError("login required")
        if resp.status_code != 200 or not data:
            raise RuntimeError(f"download failed {resp.status_code} {url}")
        if item.get("suffix") == "pdf" and not is_pdf(data):
            raise RuntimeError(f"not a pdf ({resp.headers.get('content-type')}) {url}")
        return data, item.get("title") or ""


def _search_keywords(issuer: str) -> str:
    text = _KW_KEEP.sub("", issuer or "")
    return re.sub(r"\s+", " ", text).strip()


def _parse_appendix(raw: str) -> list[tuple[str, str, str]]:
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

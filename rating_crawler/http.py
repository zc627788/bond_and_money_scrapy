# -*- coding: utf-8 -*-
from __future__ import annotations

import random
import threading
import time
from typing import Any, Optional

from curl_cffi import requests as creq

from .proxy import ProxyPool

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
IMPERSONATE = "chrome131"


class BrowserSession:
    def __init__(
        self,
        delay: float = 0.0,
        max_retries: int = 4,
        proxy_pool: Optional[ProxyPool] = None,
    ):
        self.delay = max(0.0, delay)
        self.max_retries = max_retries
        self.proxy_pool = proxy_pool
        self._local = threading.local()

    def _session(self) -> creq.Session:
        sess = getattr(self._local, "session", None)
        if sess is None:
            sess = creq.Session(impersonate=IMPERSONATE)
            self._local.session = sess
        return sess

    def _rebuild(self) -> None:
        old = getattr(self._local, "session", None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        self._local.session = creq.Session(impersonate=IMPERSONATE)
        self._local.warm = False

    def mark_warm(self) -> None:
        self._local.warm = True

    def is_warm(self) -> bool:
        return bool(getattr(self._local, "warm", False))

    def sleep(self, scale: float = 1.0) -> None:
        if self.delay <= 0:
            return
        time.sleep(self.delay * scale + random.uniform(0, self.delay * 0.2))

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        warmup: Optional[Any] = None,
        retry_403: bool = True,
        timeout: float = 12,
        **kwargs: Any,
    ) -> creq.Response:
        last_exc: Optional[Exception] = None
        last_resp: Optional[creq.Response] = None
        total_tries = self.max_retries + (1 if self.proxy_pool else 0)
        for attempt in range(1, total_tries + 1):
            use_proxy = bool(self.proxy_pool) and attempt <= self.max_retries
            proxy = self.proxy_pool.acquire() if use_proxy else None
            proxy_url = ProxyPool.as_url(proxy) if use_proxy else None
            via = f"proxy {proxy}" if proxy_url else "direct"
            try:
                extra = dict(kwargs)
                if proxy_url:
                    extra["proxy"] = proxy_url
                resp = self._session().request(
                    method,
                    url,
                    headers=headers,
                    timeout=timeout,
                    **extra,
                )
                last_resp = resp
                bad = resp.status_code in (403, 407, 412, 429, 502, 503, 504) or (
                    resp.status_code == 200 and not resp.content
                )
                if bad:
                    print(f"  [http] {attempt}/{total_tries} {via} status={resp.status_code} 换IP重试", flush=True)
                    if self.proxy_pool and use_proxy:
                        self.proxy_pool.report_bad(proxy)
                    if not retry_403 and resp.status_code in (403, 412):
                        return resp
                    if attempt == total_tries:
                        return resp
                    self._rebuild()
                    if warmup is not None:
                        warmup(self)
                    continue
                if resp.status_code >= 500:
                    print(f"  [http] {attempt}/{total_tries} {via} status={resp.status_code} 换IP重试", flush=True)
                    if self.proxy_pool and use_proxy:
                        self.proxy_pool.report_bad(proxy)
                    if attempt == total_tries:
                        return resp
                    self._rebuild()
                    continue
                return resp
            except Exception as e:
                last_exc = e
                print(f"  [http] {attempt}/{total_tries} {via} {type(e).__name__}: {e}", flush=True)
                if self.proxy_pool and use_proxy:
                    self.proxy_pool.report_bad(proxy)
                self._rebuild()
                if warmup is not None:
                    try:
                        warmup(self)
                    except Exception:
                        pass
                if attempt == total_tries:
                    break
        if last_resp is not None:
            return last_resp
        if last_exc:
            raise last_exc
        raise RuntimeError(f"request failed after {total_tries} tries: {method} {url}")

    def get(self, url: str, **kwargs: Any) -> creq.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> creq.Response:
        return self.request("POST", url, **kwargs)

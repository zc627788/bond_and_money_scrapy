# -*- coding: utf-8 -*-
from __future__ import annotations

import random
import time
from typing import Any, Optional

from curl_cffi import requests as creq

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)
IMPERSONATE = "chrome131"


class BrowserSession:
    """curl_cffi session with Chrome TLS impersonation, 403 backoff, delay."""

    def __init__(self, delay: float = 1.4, max_retries: int = 5):
        self.delay = delay
        self.max_retries = max_retries
        self.session: Optional[creq.Session] = None
        self._rebuild()

    def _rebuild(self) -> None:
        if self.session is not None:
            try:
                self.session.close()
            except Exception:
                pass
        self.session = creq.Session(impersonate=IMPERSONATE)

    def sleep(self, scale: float = 1.0) -> None:
        base = max(0.4, self.delay * scale)
        time.sleep(base + random.uniform(0, base * 0.45))

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: Optional[dict[str, str]] = None,
        warmup: Optional[Any] = None,
        retry_403: bool = True,
        **kwargs: Any,
    ) -> creq.Response:
        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_retries + 1):
            try:
                assert self.session is not None
                resp = self.session.request(method, url, headers=headers, timeout=kwargs.pop("timeout", 45), **kwargs)
                if resp.status_code in (403, 412) or (resp.status_code == 200 and not resp.content):
                    if not retry_403 or attempt == self.max_retries:
                        return resp
                    wait = min(20.0, self.delay * (2 ** attempt) + random.uniform(0.3, 1.5))
                    print(f"  [http] {resp.status_code} empty={len(resp.content)==0} {url} retry {attempt}/{self.max_retries} sleep {wait:.1f}s", flush=True)
                    self._rebuild()
                    if warmup is not None:
                        warmup(self)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    wait = min(15.0, self.delay * attempt + random.uniform(0.2, 1.0))
                    print(f"  [http] {resp.status_code} {url} retry {attempt}/{self.max_retries} sleep {wait:.1f}s", flush=True)
                    time.sleep(wait)
                    continue
                return resp
            except Exception as e:
                last_exc = e
                wait = min(15.0, self.delay * attempt + random.uniform(0.2, 1.0))
                print(f"  [http] {type(e).__name__}: {e} retry {attempt}/{self.max_retries} sleep {wait:.1f}s", flush=True)
                self._rebuild()
                if warmup is not None:
                    try:
                        warmup(self)
                    except Exception:
                        pass
                time.sleep(wait)
        if last_exc:
            raise last_exc
        raise RuntimeError(f"request failed: {method} {url}")

    def get(self, url: str, **kwargs: Any) -> creq.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> creq.Response:
        return self.request("POST", url, **kwargs)

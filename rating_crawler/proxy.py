# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import threading
import time
from typing import Optional

from curl_cffi import requests as creq

IP_PORT = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}$")


class ProxyPool:
    """线程内粘滞：同一条代理一直用到超时/失败，再换下一条。"""

    def __init__(self, api: str, max_extract: int = 50, refresh_seconds: int = 180):
        self.api = api
        self.max_extract = min(50, max(1, int(max_extract)))
        self.refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._local = threading.local()
        self._proxies: list[str] = []
        self._bad: set[str] = set()
        self._idx = 0
        self._fetched_at = 0.0
        self._min_fetch_gap = 15.0

    def acquire(self) -> Optional[str]:
        sticky = getattr(self._local, "proxy", None)
        with self._lock:
            if sticky and sticky not in self._bad:
                return sticky
            self._maybe_refresh_unlocked()
            ip = self._next_unlocked()
            if not ip:
                self._refresh_unlocked()
                ip = self._next_unlocked()
        self._local.proxy = ip
        return ip

    def release(self, proxy: Optional[str], *, bad: bool = False) -> None:
        if bad:
            self.report_bad(proxy)

    def report_bad(self, proxy: Optional[str]) -> None:
        if not proxy:
            return
        with self._lock:
            self._bad.add(proxy)
            alive = [p for p in self._proxies if p not in self._bad]
            if len(alive) <= 2:
                self._refresh_unlocked()
        if getattr(self._local, "proxy", None) == proxy:
            self._local.proxy = None

    def _next_unlocked(self) -> Optional[str]:
        alive = [p for p in self._proxies if p not in self._bad]
        if not alive:
            return None
        self._idx = self._idx % len(alive)
        ip = alive[self._idx]
        self._idx = (self._idx + 1) % len(alive)
        return ip

    def _maybe_refresh_unlocked(self) -> None:
        if not self._proxies:
            self._refresh_unlocked()
            return
        if self._fetched_at and time.time() - self._fetched_at >= self.refresh_seconds:
            self._refresh_unlocked()

    def _refresh_unlocked(self) -> None:
        if self._fetched_at and time.time() - self._fetched_at < self._min_fetch_gap:
            return
        found = self._fetch()
        if not found:
            return
        keep_bad = {ip for ip in self._bad if ip in found}
        self._proxies = found
        self._bad = keep_bad
        self._idx = 0
        self._fetched_at = time.time()
        print(
            f"  [proxy] 提取 {len(found)} 条（上限 {self.max_extract}，拉黑 {len(self._bad)}）",
            flush=True,
        )

    def _fetch(self) -> list[str]:
        if not self.api:
            return []
        url = self._capped_url()
        try:
            resp = creq.get(url, timeout=20, impersonate="chrome131")
            text = resp.text or ""
        except Exception as e:
            print(f"  [proxy] 提取失败: {type(e).__name__}: {e}", flush=True)
            return []
        found: list[str] = []
        seen: set[str] = set()
        for raw in text.replace(",", "\n").splitlines():
            line = raw.strip()
            if not IP_PORT.match(line) or line in seen:
                continue
            seen.add(line)
            found.append(line)
            if len(found) >= self.max_extract:
                break
        if not found:
            preview = text.strip().replace("\n", " ")[:180]
            print(f"  [proxy] 未解析到 IP，响应: {preview}", flush=True)
        return found

    def _capped_url(self) -> str:
        url = self.api
        if "number=" in url:
            url = re.sub(r"number=\d+", f"number={self.max_extract}", url)
        return url

    @staticmethod
    def as_url(proxy: Optional[str]) -> Optional[str]:
        if not proxy:
            return None
        if proxy.startswith("http://") or proxy.startswith("https://"):
            return proxy
        return f"http://{proxy}"

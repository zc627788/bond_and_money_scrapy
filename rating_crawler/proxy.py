# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import threading
import time
from collections import deque
from typing import Optional

from curl_cffi import requests as creq

IP_PORT = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}$")


class ProxyPool:
    """58ip 动态代理：独占租约，同一 IP 同一时刻只给一条请求。"""

    def __init__(self, api: str, max_extract: int = 50, refresh_seconds: int = 180):
        self.api = api
        self.max_extract = min(50, max(1, int(max_extract)))
        self.refresh_seconds = refresh_seconds
        self._lock = threading.Lock()
        self._free: deque[str] = deque()
        self._used: deque[str] = deque()
        self._in_use: set[str] = set()
        self._bad: set[str] = set()
        self._fetched_at = 0.0

    def acquire(self) -> Optional[str]:
        with self._lock:
            self._maybe_refresh_unlocked()
            ip = self._take_unlocked()
            if ip:
                return ip
            self._refresh_unlocked()
            return self._take_unlocked()

    def release(self, proxy: Optional[str], *, bad: bool = False) -> None:
        if not proxy:
            return
        with self._lock:
            self._in_use.discard(proxy)
            if bad:
                self._bad.add(proxy)
            elif proxy not in self._bad:
                self._used.append(proxy)
            if self._available_unlocked() <= 3:
                self._refresh_unlocked()

    def report_bad(self, proxy: Optional[str]) -> None:
        self.release(proxy, bad=True)

    def _take_unlocked(self) -> Optional[str]:
        ip = self._pop_unused(self._free)
        if ip:
            return ip
        return self._pop_unused(self._used)

    def _pop_unused(self, q: deque[str]) -> Optional[str]:
        n = len(q)
        for _ in range(n):
            ip = q.popleft()
            if ip in self._bad or ip in self._in_use:
                continue
            self._in_use.add(ip)
            return ip
        return None

    def _available_unlocked(self) -> int:
        return len(
            [p for p in list(self._free) + list(self._used) if p not in self._bad and p not in self._in_use]
        )

    def _maybe_refresh_unlocked(self) -> None:
        if not self._free and not self._used and not self._in_use:
            self._refresh_unlocked()
            return
        if self._fetched_at and time.time() - self._fetched_at >= self.refresh_seconds:
            self._refresh_unlocked()

    def _refresh_unlocked(self) -> None:
        found = self._fetch()
        if not found:
            return
        self._free = deque(ip for ip in found if ip not in self._in_use)
        self._used = deque()
        self._bad = {ip for ip in self._bad if ip in self._in_use}
        self._fetched_at = time.time()
        print(
            f"  [proxy] 提取 {len(found)} 条（上限 {self.max_extract}，占用中 {len(self._in_use)}）",
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
            if not IP_PORT.match(line):
                continue
            if line in seen:
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

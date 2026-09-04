# -*- coding: utf-8 -*-
from __future__ import annotations

import re
import threading
import time
from typing import Optional

from curl_cffi import requests as creq

IP_PORT = re.compile(r"^\d{1,3}(?:\.\d{1,3}){3}:\d{2,5}$")


class LatencyBudget:
    """用成功请求的耗时估正常值，超时 = clamp(倍数 × EWMA)。"""

    def __init__(self, *, multiplier: float, min_s: float, max_s: float, cold_s: float):
        self.multiplier = multiplier
        self.min_s = min_s
        self.max_s = max_s
        self.cold_s = cold_s
        self.ewma: dict[str, float] = {}
        self.hits: dict[str, int] = {}

    def observe(self, key: str, seconds: float) -> None:
        if not key or seconds <= 0:
            return
        n = self.hits.get(key, 0)
        if n == 0:
            self.ewma[key] = seconds
        else:
            self.ewma[key] = 0.25 * seconds + 0.75 * self.ewma[key]
        self.hits[key] = n + 1

    def typical(self, key: str) -> float:
        return float(self.ewma.get(key) or 0.0)

    def timeout(self, key: str) -> float:
        n = self.hits.get(key, 0)
        if n < 2:
            return self.cold_s
        t = self.multiplier * self.ewma[key]
        return max(self.min_s, min(self.max_s, t))


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
        self._bad_at: dict[str, float] = {}
        self._dl_hits: dict[str, int] = {}
        self._rest_until: dict[str, float] = {}
        self._in_use: set[str] = set()
        self._idx = 0
        self._fetched_at = 0.0
        self._min_fetch_gap = 15.0
        self._bad_ttl = 90.0
        # 实测：同一 IP 大约 10 个 PDF 后 403。第 8 个主动休息，别去撞第 10 下。
        self._dl_hits_before_rest = 8
        self._dl_rest_s = 120.0
        # 列表是小 JSON，可以快切；PDF 动辄几 MB，不能用列表那套秒数。
        self._api = LatencyBudget(multiplier=3.0, min_s=3.0, max_s=12.0, cold_s=8.0)
        self._dl = LatencyBudget(multiplier=4.0, min_s=60.0, max_s=180.0, cold_s=120.0)

    def acquire(self) -> Optional[str]:
        sticky = getattr(self._local, "proxy", None)
        with self._lock:
            self._expire_bad_unlocked()
            if sticky and self._blocked(sticky):
                sticky = None
                self._local.proxy = None
            if sticky:
                self._in_use.add(sticky)
                return sticky
            self._maybe_refresh_unlocked()
            ip = self._next_unlocked(free_only=True)
            if not ip:
                self._refresh_unlocked(force=True)
                self._expire_bad_unlocked()
                ip = self._next_unlocked(free_only=True)
            if not ip:
                ip = self._next_unlocked(free_only=False)
            if ip:
                self._in_use.add(ip)
                self._local.proxy = ip
                return ip
            wait_s = self._retry_wait_unlocked()
        if wait_s > 0:
            time.sleep(min(15.0, max(1.0, wait_s)))
            with self._lock:
                self._refresh_unlocked(force=True)
                self._expire_bad_unlocked()
                ip = self._next_unlocked(free_only=True) or self._next_unlocked(free_only=False)
                if ip:
                    self._in_use.add(ip)
                self._local.proxy = ip
                return ip
        self._local.proxy = None
        return None

    def retry_wait(self) -> float:
        with self._lock:
            self._expire_bad_unlocked()
            return self._retry_wait_unlocked()

    def _retry_wait_unlocked(self) -> float:
        now = time.time()
        waits: list[float] = []
        if self._fetched_at:
            waits.append(max(0.0, self._min_fetch_gap - (now - self._fetched_at)))
        if self._bad_at:
            waits.append(max(0.0, min(self._bad_ttl - (now - t) for t in self._bad_at.values())))
        if self._rest_until:
            waits.append(max(0.0, min(ts - now for ts in self._rest_until.values())))
        return min(waits) if waits else self._min_fetch_gap

    def refresh(self, *, force: bool = False) -> None:
        with self._lock:
            self._refresh_unlocked(force=force)

    def release(self, proxy: Optional[str], *, bad: bool = False) -> None:
        if bad:
            self.report_bad(proxy)

    def observe_ok(self, proxy: Optional[str], seconds: float, budget: str = "api") -> None:
        key = proxy or "direct"
        with self._lock:
            self._budget(budget).observe(key, seconds)
            if budget == "download" and proxy:
                n = int(self._dl_hits.get(proxy, 0)) + 1
                self._dl_hits[proxy] = n
                if n >= self._dl_hits_before_rest and proxy not in self._rest_until:
                    self._rest_until[proxy] = time.time() + self._dl_rest_s
                    self._in_use.discard(proxy)
                    print(
                        f"  [proxy] 线路 {proxy.split(':')[0]} 已下 {n} 个，休息 {int(self._dl_rest_s)}s，换下一条",
                        flush=True,
                    )
        if (
            budget == "download"
            and proxy
            and getattr(self._local, "proxy", None) == proxy
            and proxy in self._rest_until
        ):
            self._local.proxy = None

    def timeout_for(self, proxy: Optional[str], budget: str = "api") -> float:
        key = proxy or "direct"
        with self._lock:
            return self._budget(budget).timeout(key)

    def typical_for(self, proxy: Optional[str], budget: str = "api") -> float:
        key = proxy or "direct"
        with self._lock:
            return self._budget(budget).typical(key)

    def _budget(self, budget: str) -> LatencyBudget:
        return self._dl if budget == "download" else self._api

    def report_bad(self, proxy: Optional[str]) -> None:
        if not proxy:
            return
        with self._lock:
            self._bad.add(proxy)
            self._bad_at[proxy] = time.time()
            self._dl_hits.pop(proxy, None)
            self._rest_until.pop(proxy, None)
            self._in_use.discard(proxy)
            self._expire_bad_unlocked()
            alive = [p for p in self._proxies if not self._blocked(p)]
            if len(alive) <= 2:
                self._refresh_unlocked(force=True)
        if getattr(self._local, "proxy", None) == proxy:
            self._local.proxy = None

    def _blocked(self, ip: str) -> bool:
        return ip in self._bad or ip in self._rest_until

    def _next_unlocked(self, free_only: bool = False) -> Optional[str]:
        alive = [p for p in self._proxies if not self._blocked(p)]
        if free_only:
            alive = [p for p in alive if p not in self._in_use]
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

    def _expire_bad_unlocked(self) -> None:
        now = time.time()
        for ip, ts in list(self._bad_at.items()):
            if now - ts >= self._bad_ttl:
                self._bad.discard(ip)
                self._bad_at.pop(ip, None)
        for ip, ts in list(self._rest_until.items()):
            if now >= ts:
                self._rest_until.pop(ip, None)
                self._dl_hits[ip] = 0

    def _refresh_unlocked(self, force: bool = False) -> None:
        gap = self._min_fetch_gap
        if self._fetched_at and time.time() - self._fetched_at < gap:
            return
        found = self._fetch()
        self._fetched_at = time.time()
        if not found:
            return
        self._expire_bad_unlocked()
        merged: list[str] = []
        seen: set[str] = set()
        for ip in found + self._proxies:
            if ip in seen:
                continue
            seen.add(ip)
            merged.append(ip)
            if len(merged) >= 80:
                break
        self._proxies = merged
        self._idx = 0
        alive = len([p for p in self._proxies if not self._blocked(p)])
        print(
            f"  [proxy] 提取 {len(found)} 条（池 {len(self._proxies)}，可用 {alive}，拉黑 {len(self._bad)}，休息 {len(self._rest_until)}）",
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

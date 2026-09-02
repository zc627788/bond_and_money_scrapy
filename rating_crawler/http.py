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
DOWNLOAD_TIMEOUT = 10
DOWNLOAD_PROXY_TRIES = 2
DOWNLOAD_DIRECT_TRIES = 1


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

    def bind_progress(self, cb: Optional[Any]) -> None:
        self._local.progress = cb

    def _notify(self, payload: dict[str, Any]) -> None:
        print(f"  [http] {payload.get('label')}", flush=True)
        cb = getattr(self._local, "progress", None)
        if not cb:
            return
        try:
            cb(payload)
        except Exception:
            pass

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
        proxy_tries: Optional[int] = None,
        direct_tries: Optional[int] = None,
        retry_timeouts_only: bool = False,
        **kwargs: Any,
    ) -> creq.Response:
        last_exc: Optional[Exception] = None
        last_resp: Optional[creq.Response] = None
        kinds = _attempt_kinds(
            has_pool=bool(self.proxy_pool),
            proxy_tries=self.max_retries if proxy_tries is None else proxy_tries,
            direct_tries=1 if direct_tries is None else direct_tries,
        )
        total_tries = len(kinds)
        on_attempt = kwargs.pop("on_attempt", None)
        for attempt, kind in enumerate(kinds, 1):
            use_proxy = kind == "proxy" and bool(self.proxy_pool)
            proxy = self.proxy_pool.acquire() if use_proxy else None
            proxy_url = ProxyPool.as_url(proxy) if use_proxy else None
            nxt = kinds[attempt] if attempt < total_tries else ""
            info = {
                "event": "try",
                "kind": kind,
                "attempt": attempt,
                "total": total_tries,
                "proxy": proxy or "",
                "next_kind": nxt,
                "status": "proxy" if kind == "proxy" else "direct",
                "label": format_attempt(
                    event="try",
                    kind=kind,
                    attempt=attempt,
                    total=total_tries,
                    proxy=proxy or "",
                ),
            }
            self._notify(info)
            if on_attempt:
                try:
                    on_attempt(info)
                except Exception:
                    pass
            try:
                extra = dict(kwargs)
                extra.pop("proxy", None)
                extra.pop("proxies", None)
                extra.pop("on_attempt", None)
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
                if _looks_like_login(resp):
                    info = {
                        "event": "login",
                        "kind": kind,
                        "attempt": attempt,
                        "total": total_tries,
                        "proxy": proxy or "",
                        "next_kind": "",
                        "status": "locked",
                        "reason": "需登录",
                        "label": format_attempt(
                            event="login",
                            kind=kind,
                            attempt=attempt,
                            total=total_tries,
                            proxy=proxy or "",
                            reason="需登录",
                        ),
                    }
                    self._notify(info)
                    if on_attempt:
                        try:
                            on_attempt(info)
                        except Exception:
                            pass
                    return resp
                bad = resp.status_code in (403, 407, 412, 429, 502, 503, 504) or (
                    resp.status_code == 200 and not resp.content
                )
                if bad or resp.status_code >= 500:
                    reason = "空响应" if resp.status_code == 200 and not resp.content else f"HTTP {resp.status_code}"
                    info = {
                        "event": "retry",
                        "kind": kind,
                        "attempt": attempt,
                        "total": total_tries,
                        "proxy": proxy or "",
                        "next_kind": nxt,
                        "status": "retry",
                        "reason": reason,
                        "label": format_attempt(
                            event="retry",
                            kind=kind,
                            attempt=attempt,
                            total=total_tries,
                            proxy=proxy or "",
                            reason=reason,
                            next_kind=nxt,
                        ),
                    }
                    self._notify(info)
                    if on_attempt:
                        try:
                            on_attempt(info)
                        except Exception:
                            pass
                    if self.proxy_pool and use_proxy:
                        self.proxy_pool.report_bad(proxy)
                    if retry_timeouts_only:
                        if attempt == total_tries:
                            return resp
                        self._rebuild()
                        if warmup is not None:
                            try:
                                warmup(self)
                            except Exception:
                                pass
                        continue
                    if not retry_403 and resp.status_code in (403, 412):
                        return resp
                    if attempt == total_tries:
                        return resp
                    self._rebuild()
                    if warmup is not None:
                        warmup(self)
                    continue
                return resp
            except Exception as e:
                last_exc = e
                reason = "超时" if _timeout_like(e) else f"{type(e).__name__}"
                info = {
                    "event": "retry",
                    "kind": kind,
                    "attempt": attempt,
                    "total": total_tries,
                    "proxy": proxy or "",
                    "next_kind": nxt,
                    "status": "retry",
                    "reason": reason,
                    "label": format_attempt(
                        event="retry",
                        kind=kind,
                        attempt=attempt,
                        total=total_tries,
                        proxy=proxy or "",
                        reason=reason,
                        next_kind=nxt,
                    ),
                }
                self._notify(info)
                if on_attempt:
                    try:
                        on_attempt(info)
                    except Exception:
                        pass
                if retry_timeouts_only and not _timeout_like(e):
                    raise
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

    def get_file(self, url: str, **kwargs: Any) -> creq.Response:
        kwargs.setdefault("timeout", DOWNLOAD_TIMEOUT)
        kwargs.setdefault("proxy_tries", DOWNLOAD_PROXY_TRIES)
        kwargs.setdefault("direct_tries", DOWNLOAD_DIRECT_TRIES)
        kwargs.setdefault("retry_timeouts_only", True)
        return self.get(url, **kwargs)


def _attempt_kinds(*, has_pool: bool, proxy_tries: int, direct_tries: int) -> list[str]:
    kinds: list[str] = []
    if has_pool:
        kinds.extend(["proxy"] * max(0, int(proxy_tries)))
    kinds.extend(["direct"] * max(0, int(direct_tries)))
    return kinds or ["direct"]


def format_attempt(
    *,
    event: str,
    kind: str,
    attempt: int,
    total: int,
    proxy: str = "",
    reason: str = "",
    next_kind: str = "",
) -> str:
    ip = (proxy or "").split(":")[0] if kind == "proxy" else ""
    if kind == "proxy":
        via = f"走代理 {ip}".strip() if ip else "走代理"
    else:
        via = "走直连"
    slot = f"{attempt}/{total}"
    if event == "try":
        return f"{via} {slot}"
    if event == "login":
        return f"{via} {slot} · 需登录，停止"
    if next_kind == "proxy":
        action = f"换代理 {attempt + 1}/{total}"
    elif next_kind == "direct":
        action = f"改直连 {attempt + 1}/{total}"
    else:
        action = "已用尽"
    why = reason or "失败"
    return f"{via} {slot} · {why}，{action}"


def _timeout_like(exc: Exception) -> bool:
    text = f"{type(exc).__name__} {exc}".lower()
    keys = (
        "timeout",
        "timed out",
        "time-out",
        "connect",
        "connection",
        "proxy",
        "reset",
        "refused",
        "hangup",
        "unreachable",
        "curl: (28)",
        "curl: (7)",
        "curl: (56)",
    )
    return any(k in text for k in keys)


def _looks_like_login(resp: creq.Response) -> bool:
    if resp.status_code in (401,):
        return True
    ctype = (resp.headers.get("content-type") or "").lower()
    data = resp.content or b""
    if not data:
        return False
    head = data[:4000].lower()
    if b"quanxian" in head or "权限".encode("utf-8") in data[:4000]:
        return True
    if "html" not in ctype and not head.lstrip().startswith(b"<!doctype") and not head.lstrip().startswith(b"<html"):
        return False
    return (
        "登录".encode("utf-8") in data[:4000]
        or b"login" in head
        or b"signin" in head
    )

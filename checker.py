"""Checks a single URL: follows redirects hop-by-hop, classifies errors.

Nothing here tries to evade rate limits, WAFs or any other protection: requests
are throttled per host and failures are simply reported.
"""
from __future__ import annotations

import asyncio
import re
import socket
import ssl
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urldefrag, urljoin, urlsplit

import httpx

from models import Hop, LOOP_ERRORS, UrlResult
from urlutils import bare_host, host_of

RETRYABLE_ERRORS = {"timeout", "connection_error"}
RETRYABLE_STATUS = {429, 502, 503, 504}


# --------------------------------------------------------------------------
# HTTP client + throttling
# --------------------------------------------------------------------------
def make_client(cfg: Dict[str, Any]) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={
            "User-Agent": cfg["user_agent"],
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Accept-Language": "en",
        },
        timeout=httpx.Timeout(cfg["timeout"]),
        limits=httpx.Limits(max_connections=cfg["max_connections"], max_keepalive_connections=20),
        follow_redirects=False,   # we follow redirects ourselves to record the chain
        verify=True,
        trust_env=cfg["trust_env"],
    )


class HostThrottle:
    """Limits requests per host: max N at once and a minimum gap between starts."""

    def __init__(self, delay: float, per_host: int):
        self.delay = delay
        self.per_host = per_host
        self._sems: Dict[str, asyncio.Semaphore] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._next: Dict[str, float] = {}
        self._delay_override: Dict[str, float] = {}

    def set_min_delay(self, host: str, delay: float) -> None:
        """Used for robots.txt Crawl-delay."""
        self._delay_override[host] = max(delay, self._delay_override.get(host, 0.0))

    @asynccontextmanager
    async def slot(self, host: str):
        sem = self._sems.setdefault(host, asyncio.Semaphore(self.per_host))
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with sem:
            async with lock:
                now = time.monotonic()
                wait = self._next.get(host, 0.0) - now
                if wait > 0:
                    await asyncio.sleep(wait)
                    now = time.monotonic()
                self._next[host] = now + max(self.delay, self._delay_override.get(host, 0.0))
            yield


# --------------------------------------------------------------------------
# Error classification
# --------------------------------------------------------------------------
_DNS_HINTS = (
    "name or service not known", "nodename nor servname", "getaddrinfo failed",
    "temporary failure in name resolution", "no address associated with hostname",
    "name resolution",
)


def _exception_chain(exc: BaseException) -> List[BaseException]:
    chain: List[BaseException] = []
    cur: Optional[BaseException] = exc
    while cur is not None and all(cur is not seen for seen in chain):
        chain.append(cur)
        cur = cur.__cause__ or cur.__context__
    return chain


def classify_exception(exc: BaseException) -> Tuple[str, str]:
    """Map an exception to (error_type, readable detail)."""
    chain = _exception_chain(exc)
    text = " | ".join(str(e) for e in chain).lower()

    if isinstance(exc, (httpx.TimeoutException, asyncio.TimeoutError)):
        return "timeout", "No response within the time limit (%s)" % type(exc).__name__
    ssl_exc = next((e for e in chain if isinstance(e, ssl.SSLError)), None)
    if ssl_exc is not None or "certificate verify failed" in text or "ssl:" in text:
        return "ssl_error", ("SSL/TLS error: %s" % (ssl_exc or exc))[:300]
    if any(isinstance(e, socket.gaierror) for e in chain) or any(h in text for h in _DNS_HINTS):
        root = next((e for e in chain if isinstance(e, socket.gaierror)), exc)
        return "dns_error", ("DNS lookup failed: %s" % root)[:300]
    if isinstance(exc, (httpx.InvalidURL, httpx.UnsupportedProtocol)):
        return "invalid_url", str(exc)[:300]
    if isinstance(exc, httpx.TransportError):
        return "connection_error", ("%s: %s" % (type(exc).__name__, exc))[:300]
    return "other_error", ("%s: %s" % (type(exc).__name__, exc))[:300]


def status_error(status: int, reason: str) -> Tuple[str, str]:
    if 200 <= status < 300:
        return "", ""
    detail = ("HTTP %d %s" % (status, reason or "")).strip()
    if status >= 500:
        return ("http_500" if status == 500 else "http_5xx"), detail
    if status >= 400:
        return ("http_404" if status == 404 else "http_4xx"), detail
    return "unexpected_status", "Unexpected HTTP status %d" % status


# --------------------------------------------------------------------------
# Redirect classification (pure functions, easy to test)
# --------------------------------------------------------------------------
def hop_is_expected(src: str, dst: str, exp: Dict[str, Any]) -> bool:
    """Is a single redirect src -> dst one the user has said is normal?"""
    for rule in exp.get("rules", []):
        if re.search(rule["from"], src) and (not rule.get("to") or re.search(rule["to"], dst)):
            return True
    try:
        a, b = urlsplit(src), urlsplit(dst)
    except ValueError:
        return False
    ha, hb = (a.hostname or "").lower(), (b.hostname or "").lower()
    if a.query != b.query or a.path.rstrip("/") != b.path.rstrip("/"):
        return False
    if bare_host(ha) != bare_host(hb):
        return False
    scheme_change = a.scheme.lower() != b.scheme.lower()
    host_change = ha != hb
    slash_change = a.path != b.path
    if not (scheme_change or host_change or slash_change):
        return False
    if scheme_change and not (a.scheme.lower() == "http" and b.scheme.lower() == "https"
                              and exp.get("http_to_https", True)):
        return False
    if host_change and not exp.get("www_redirects", True):
        return False
    if slash_change and not exp.get("trailing_slash", True):
        return False
    return True


def categorize_redirect(urls: List[str], loop: bool, same_domain: Callable[[str], bool],
                        max_chain: int, exp: Dict[str, Any]) -> str:
    """urls = every URL visited in order. Returns '', expected, attention, loop,
    cross_domain or excessive."""
    if loop:
        return "loop"
    if len(urls) < 2:
        return ""
    final_host = host_of(urls[-1])
    allowed = {bare_host(d.lower()) for d in exp.get("allowed_cross_domains", [])}
    if not same_domain(final_host) and bare_host(final_host) not in allowed:
        return "cross_domain"
    if len(urls) - 1 > max_chain:
        return "excessive"
    if all(hop_is_expected(urls[i], urls[i + 1], exp) for i in range(len(urls) - 1)):
        return "expected"
    return "attention"


# --------------------------------------------------------------------------
# The checker
# --------------------------------------------------------------------------
@dataclass
class HopResponse:
    status: int
    reason: str
    location: Optional[str]
    content_type: str
    elapsed: float
    body: Optional[bytes]


class RequestFailed(Exception):
    def __init__(self, exc: BaseException, elapsed: float):
        self.etype, self.detail = classify_exception(exc)
        super().__init__(self.detail)
        self.exc = exc
        self.elapsed = elapsed


class Checker:
    def __init__(self, client: httpx.AsyncClient, throttle: HostThrottle, cfg: Dict[str, Any]):
        self.client = client
        self.throttle = throttle
        self.cfg = cfg
        self.total_timeout = cfg["timeout"] * 2

    async def _do_request(self, method: str, url: str, want_body: bool, t0: float) -> HopResponse:
        async with self.client.stream(method, url) as resp:
            status = resp.status_code
            ctype = resp.headers.get("content-type", "")
            location = resp.headers.get("location") if 300 <= status < 400 else None
            body = None
            if want_body and 200 <= status < 300 and "html" in ctype.lower():
                chunks, size = [], 0
                async for chunk in resp.aiter_bytes():
                    chunks.append(chunk)
                    size += len(chunk)
                    if size >= self.cfg["max_body_bytes"]:
                        break
                body = b"".join(chunks)
            return HopResponse(status, resp.reason_phrase or "", location, ctype,
                               time.monotonic() - t0, body)

    async def _request_once(self, method: str, url: str, want_body: bool) -> HopResponse:
        async with self.throttle.slot(host_of(url)):
            t0 = time.monotonic()      # start the clock after any politeness delay
            try:
                return await asyncio.wait_for(
                    self._do_request(method, url, want_body, t0), timeout=self.total_timeout)
            except Exception as exc:
                raise RequestFailed(exc, time.monotonic() - t0)

    async def _request_with_retries(self, method: str, url: str, want_body: bool) -> HopResponse:
        attempt = 0
        while True:
            try:
                resp = await self._request_once(method, url, want_body)
            except RequestFailed as fail:
                if fail.etype in RETRYABLE_ERRORS and attempt < self.cfg["retries"]:
                    attempt += 1
                    await asyncio.sleep(self.cfg["retry_backoff"] * (2 ** (attempt - 1)))
                    continue
                raise
            if resp.status in RETRYABLE_STATUS and attempt < self.cfg["retries"]:
                attempt += 1
                await asyncio.sleep(self.cfg["retry_backoff"] * (2 ** (attempt - 1)))
                continue
            return resp

    async def check(self, website: str, url: str, kind: str = "page", want_body: bool = False,
                    is_start: bool = False, external: bool = False,
                    same_domain: Optional[Callable[[str], bool]] = None
                    ) -> Tuple[UrlResult, Optional[bytes]]:
        """Fetch url, following redirects manually. Returns (result, html_bytes_or_None)."""
        cfg = self.cfg
        if same_domain is None:
            own = bare_host(host_of(url))
            same_domain = lambda h: bare_host(h) == own  # noqa: E731

        result = UrlResult(website=website, url=url, kind=kind, external=external, is_start=is_start)
        # Pages we crawl use GET; assets and external links use the lighter HEAD
        # (falling back to GET when a server rejects HEAD).
        method = "GET" if (kind == "page" and not external) else "HEAD"

        hops: List[Hop] = []
        visited = [url]
        current = url
        body: Optional[bytes] = None
        total = 0.0
        error_type = error_detail = ""

        while True:
            try:
                resp = await self._request_with_retries(method, current, want_body)
                if method == "HEAD" and resp.status >= 400:
                    resp = await self._request_with_retries("GET", current, False)
            except RequestFailed as fail:
                hops.append(Hop(current, None, None, fail.elapsed))
                total += fail.elapsed
                error_type, error_detail = fail.etype, fail.detail
                if len(hops) > 1:
                    error_detail += " (while following redirect from %s)" % hops[-2].url
                break

            total += resp.elapsed
            if 300 <= resp.status < 400 and resp.status != 304:
                if not resp.location:
                    hops.append(Hop(current, resp.status, None, resp.elapsed))
                    error_type = "bad_redirect"
                    error_detail = "HTTP %d without a Location header" % resp.status
                    break
                try:
                    nxt = urldefrag(urljoin(current, resp.location))[0]
                    scheme = urlsplit(nxt).scheme.lower()
                except ValueError:
                    nxt, scheme = resp.location, ""
                hops.append(Hop(current, resp.status, nxt, resp.elapsed))
                if scheme not in ("http", "https"):
                    error_type = "bad_redirect"
                    error_detail = "Redirects to an unsupported URL: %s" % nxt
                    break
                if nxt in visited:
                    error_type = "redirect_loop"
                    error_detail = "Redirect loop: " + " \u2192 ".join(visited + [nxt])
                    break
                if len(hops) > cfg["max_redirects"]:
                    error_type = "redirect_limit"
                    error_detail = "More than %d redirects" % cfg["max_redirects"]
                    break
                visited.append(nxt)
                current = nxt
                continue

            hops.append(Hop(current, resp.status, None, resp.elapsed))
            result.content_type = resp.content_type
            error_type, error_detail = status_error(resp.status, resp.reason)
            if not error_type:
                body = resp.body
            break

        last = hops[-1]
        result.chain = hops
        result.status = last.status
        result.final_url = last.url
        result.redirect_count = sum(1 for h in hops if h.location)
        result.final_same_domain = same_domain(host_of(last.url))
        result.error_type, result.error_detail = error_type, error_detail
        result.response_time = total
        result.slow = error_type != "timeout" and total >= cfg["slow_threshold"]
        if result.redirect_count > 0:
            result.redirect_category = categorize_redirect(
                [h.url for h in hops], error_type in LOOP_ERRORS, same_domain,
                cfg["max_redirect_chain"], cfg["expected_redirects"])
        return result, (body if not error_type else None)

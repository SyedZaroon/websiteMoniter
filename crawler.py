"""Queue-based, polite crawler for one website."""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Set, Tuple
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

import httpx
from bs4 import BeautifulSoup

from checker import Checker, HostThrottle
from models import SiteReport, UrlResult
from urlutils import (NON_HTML_EXTENSIONS, bare_host, clean_url, dedupe_key,
                      ensure_scheme, host_of, path_extension)


# --------------------------------------------------------------------------
# HTML parsing
# --------------------------------------------------------------------------
@dataclass
class PageInfo:
    links: List[str] = field(default_factory=list)                 # <a href>, <area href>
    assets: List[Tuple[str, str]] = field(default_factory=list)    # (hint, url): img / asset
    canonical: str = ""


def _srcset_urls(value: str) -> List[str]:
    out = []
    for tok in (value or "").split():
        tok = tok.strip(",")
        if tok and not re.fullmatch(r"\d+(\.\d+)?[wx]", tok):
            out.append(tok)
    return out


def extract_page_info(body: bytes, base_url: str) -> PageInfo:
    soup = BeautifulSoup(body, "html.parser")
    base = base_url
    base_tag = soup.find("base", href=True)
    if base_tag:
        base = urljoin(base_url, str(base_tag["href"]).strip())

    def resolve(value: Any) -> Optional[str]:
        value = (value or "").strip() if isinstance(value, str) else ""
        if not value or value.startswith("#"):
            return None
        try:
            absolute = urljoin(base, value)
            if urlsplit(absolute).scheme.lower() not in ("http", "https"):
                return None   # mailto:, tel:, javascript:, data: ...
        except ValueError:
            return None
        return absolute

    info = PageInfo()
    for tag in soup.find_all(["a", "area"], href=True):
        u = resolve(tag.get("href"))
        if u:
            info.links.append(u)
    for tag in soup.find_all("img"):
        for attr in ("src", "data-src"):
            u = resolve(tag.get(attr))
            if u:
                info.assets.append(("img", u))
        for attr in ("srcset", "data-srcset"):
            for raw in _srcset_urls(tag.get(attr, "")):
                u = resolve(raw)
                if u:
                    info.assets.append(("img", u))
    for tag in soup.find_all("source"):
        for raw in _srcset_urls(tag.get("srcset", "")):
            u = resolve(raw)
            if u:
                info.assets.append(("img", u))
    for tag in soup.find_all("script", src=True):
        u = resolve(tag.get("src"))
        if u:
            info.assets.append(("asset", u))
    for tag in soup.find_all("link", href=True):
        rel = tag.get("rel") or []
        rel = rel.split() if isinstance(rel, str) else rel
        rel = [r.lower() for r in rel]
        u = resolve(tag.get("href"))
        if not u:
            continue
        if "canonical" in rel:
            info.canonical = u
        elif "stylesheet" in rel or "icon" in rel:
            info.assets.append(("asset", u))
    return info


# --------------------------------------------------------------------------
# Crawler
# --------------------------------------------------------------------------
class SiteCrawler:
    def __init__(self, site_url: str, cfg: Dict[str, Any], client: httpx.AsyncClient,
                 throttle: HostThrottle, log: Optional[Callable[[str], None]] = None):
        self.site_url = site_url
        self.cfg = cfg
        self.client = client
        self.throttle = throttle
        self.log = log or (lambda msg: None)
        self.checker = Checker(client, throttle, cfg)

        self.start_url = clean_url(ensure_scheme(site_url), cfg["ignore_query"],
                                   cfg["ignored_query_params"])
        self.host = host_of(self.start_url)
        self.scope = {bare_host(self.host)} if self.host else set()
        self.scope |= {bare_host(d.strip().lower()) for d in cfg["allowed_domains"] if d.strip()}
        self.ignore_re = [re.compile(p) for p in cfg["ignore_patterns"]]
        self.allow_re = [re.compile(p) for p in cfg["allow_patterns"]]

        self.found_on: Dict[str, List[str]] = {}      # dedupe key -> referring pages
        self.results: List[UrlResult] = []
        self.skipped: List[Dict[str, str]] = []
        self.skip_counts: Dict[str, int] = {}
        self.query_variants: Dict[Tuple[str, str], Set[str]] = {}
        self.pages_queued = self.assets_queued = self.external_queued = 0
        self._robots: Dict[str, Optional[RobotFileParser]] = {}
        self._robots_lock = asyncio.Lock()
        self._deadline: Optional[float] = None
        self._time_limited = 0
        self.queue: "asyncio.Queue[Tuple[str, str, bool, int, bool]]" = asyncio.Queue()

    # ---- scope -----------------------------------------------------------
    def in_scope(self, host: str) -> bool:
        b = bare_host(host)
        if not b:
            return False
        if b in self.scope:
            return True
        return self.cfg["include_subdomains"] and any(b.endswith("." + s) for s in self.scope)

    # ---- bookkeeping -----------------------------------------------------
    def _skip(self, url: str, reason: str, referrer: Optional[str]) -> None:
        self.skip_counts[reason] = self.skip_counts.get(reason, 0) + 1
        if len(self.skipped) < 1000:
            self.skipped.append({"url": url, "reason": reason, "found_on": referrer or ""})

    def _consider(self, raw_url: str, hint: str, referrer: Optional[str], depth: int) -> None:
        """Decide whether a discovered URL should be checked, and queue it."""
        cfg = self.cfg
        url = clean_url(raw_url, cfg["ignore_query"], cfg["ignored_query_params"])
        if not url:
            return
        key = dedupe_key(url)
        refs = self.found_on.get(key)
        if refs is not None:                     # already seen: just remember who links to it
            if referrer and referrer not in refs and len(refs) < 5:
                refs.append(referrer)
            return
        self.found_on[key] = [referrer] if referrer else []

        internal = self.in_scope(host_of(url))
        external = not internal
        if hint in ("link", "canonical"):
            kind = "asset" if (internal and path_extension(url) in NON_HTML_EXTENSIONS) else "page"
        else:
            kind = "asset"

        if any(r.search(url) for r in self.ignore_re):
            return self._skip(url, "matches an ignore pattern", referrer)

        if external:
            if kind == "asset":
                if not (hint == "img" and cfg["check_external_images"]):
                    return self._skip(url, "external asset (not checked)", referrer)
            elif not cfg["check_external_links"]:
                return self._skip(url, "external link (not checked)", referrer)
            if kind == "page":
                if self.external_queued >= cfg["max_external_links"]:
                    return self._skip(url, "external link limit reached", referrer)
                self.external_queued += 1
            else:
                if self.assets_queued >= cfg["max_assets"]:
                    return self._skip(url, "asset limit reached", referrer)
                self.assets_queued += 1
        elif kind == "asset":
            if not cfg["check_assets"]:
                return self._skip(url, "assets not checked", referrer)
            if self.assets_queued >= cfg["max_assets"]:
                return self._skip(url, "asset limit reached", referrer)
            self.assets_queued += 1
        else:
            if self.allow_re and not any(r.search(url) for r in self.allow_re):
                return self._skip(url, "does not match allow_patterns", referrer)
            if depth > cfg["max_depth"]:
                return self._skip(url, "max depth reached", referrer)
            parts = urlsplit(url)
            if parts.query:
                variants = self.query_variants.setdefault((parts.netloc, parts.path), set())
                if parts.query not in variants and len(variants) >= cfg["max_query_variants"]:
                    return self._skip(url, "too many query variants of one path", referrer)
                variants.add(parts.query)
            if self.pages_queued >= cfg["max_pages"]:
                return self._skip(url, "page limit reached", referrer)
            self.pages_queued += 1
        self.queue.put_nowait((url, kind, external, depth, False))

    # ---- robots.txt --------------------------------------------------------
    async def _robots_for(self, url: str) -> Optional[RobotFileParser]:
        parts = urlsplit(url)
        origin = "%s://%s" % (parts.scheme, parts.netloc)
        async with self._robots_lock:
            if origin in self._robots:
                return self._robots[origin]
            parser: Optional[RobotFileParser] = None
            try:
                async with self.throttle.slot(parts.hostname or ""):
                    resp = await self.client.get(origin + "/robots.txt", follow_redirects=True)
                if resp.status_code == 200:
                    parser = RobotFileParser()
                    parser.parse(resp.text.splitlines())
                    parser.modified()
                    delay = parser.crawl_delay(self.cfg["user_agent"])
                    if delay:
                        self.throttle.set_min_delay(parts.hostname or "", min(float(delay), 10.0))
            except Exception:
                parser = None      # robots.txt unreadable -> behave as if there is none
            self._robots[origin] = parser
            return parser

    async def _robots_allows(self, url: str) -> bool:
        if not self.cfg["respect_robots"]:
            return True
        parser = await self._robots_for(url)
        return True if parser is None else parser.can_fetch(self.cfg["user_agent"], url)

    # ---- workers -----------------------------------------------------------
    async def _process(self, item: Tuple[str, str, bool, int, bool]) -> None:
        url, kind, external, depth, is_start = item
        key = dedupe_key(url)
        referrers = self.found_on.get(key, [])

        if self._deadline is not None and time.monotonic() > self._deadline:
            self._time_limited += 1
            return
        if kind == "page" and not external and not is_start and not await self._robots_allows(url):
            return self._skip(url, "blocked by robots.txt", referrers[0] if referrers else None)

        want_body = kind == "page" and not external
        if external:
            own = bare_host(host_of(url))
            same_domain = lambda h: bare_host(h) == own  # noqa: E731
        else:
            same_domain = self.in_scope
        result, body = await self.checker.check(
            self.site_url, url, kind=kind, want_body=want_body, is_start=is_start,
            external=external, same_domain=same_domain)
        result.depth = depth
        result.found_on = list(self.found_on.get(key, []))
        self.results.append(result)
        if len(self.results) % 50 == 0:
            self.log("  %s: %d URLs checked, %d problems so far"
                     % (self.host, len(self.results), sum(1 for r in self.results if r.is_error)))

        if body and want_body and self.in_scope(host_of(result.final_url)):
            loop = asyncio.get_running_loop()
            info = await loop.run_in_executor(None, extract_page_info, body, result.final_url)
            if info.canonical:
                result.canonical = info.canonical
                if not self.in_scope(host_of(info.canonical)):
                    result.notes.append("Canonical URL points to a different domain: " + info.canonical)
                self._consider(info.canonical, "canonical", result.url, depth + 1)
            for href in info.links:
                self._consider(href, "link", result.url, depth + 1)
            for hint, asset in info.assets:
                self._consider(asset, hint, result.url, depth + 1)

    async def _worker(self) -> None:
        while True:
            item = await self.queue.get()
            try:
                await self._process(item)
            except asyncio.CancelledError:
                raise
            except Exception as exc:   # never let one bad URL kill the crawl
                self.log("  ! internal error on %s: %s: %s" % (item[0], type(exc).__name__, exc))
            finally:
                self.queue.task_done()

    # ---- entry point ---------------------------------------------------------
    async def run(self) -> SiteReport:
        report = SiteReport(website=self.site_url, host=self.host)
        t0 = time.monotonic()
        if not self.start_url:
            report.results.append(UrlResult(
                website=self.site_url, url=self.site_url, is_start=True, error_type="invalid_url",
                error_detail="Not a valid http(s) URL"))
            return report

        limit = self.cfg["site_time_limit"]
        self._deadline = (t0 + limit) if limit else None
        self.found_on[dedupe_key(self.start_url)] = []
        self.pages_queued = 1
        self.queue.put_nowait((self.start_url, "page", False, 0, True))

        workers = [asyncio.create_task(self._worker()) for _ in range(self.cfg["concurrency"])]
        try:
            await self.queue.join()
        finally:
            for w in workers:
                w.cancel()
            await asyncio.gather(*workers, return_exceptions=True)

        report.results = self.results
        report.skipped = self.skipped
        report.skip_counts = dict(self.skip_counts)
        report.discovered = len(self.found_on)
        report.duration = time.monotonic() - t0
        if self.skip_counts.get("page limit reached"):
            report.notes.append(
                "Page limit (%d) reached - %d more page URL(s) were found but not checked. "
                "Raise max_pages to check more." % (self.cfg["max_pages"], self.skip_counts["page limit reached"]))
        if self._time_limited:
            report.notes.append("Time limit (%ds) reached - %d queued URL(s) were not checked."
                                % (limit, self._time_limited))
        return report

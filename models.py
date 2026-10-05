"""Plain data classes shared by the checker, crawler and reporter."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

LOOP_ERRORS = ("redirect_loop", "redirect_limit")
CONNECTION_ERRORS = ("timeout", "connection_error")
SSL_DNS_ERRORS = ("ssl_error", "dns_error")
OTHER_ERRORS = ("bad_redirect", "invalid_url", "unexpected_status", "other_error")
UNEXPECTED_REDIRECTS = ("attention", "loop", "cross_domain", "excessive")

ERROR_LABELS = {
    "http_404": "404 Not Found",
    "http_500": "500 Internal Server Error",
    "http_4xx": "Other 4xx client error",
    "http_5xx": "Other 5xx server error",
    "timeout": "Timeout",
    "connection_error": "Connection error",
    "dns_error": "DNS error",
    "ssl_error": "SSL/TLS error",
    "redirect_loop": "Redirect loop",
    "redirect_limit": "Too many redirects",
    "bad_redirect": "Bad redirect",
    "invalid_url": "Invalid URL",
    "unexpected_status": "Unexpected status",
    "other_error": "Other error",
}

REDIRECT_LABELS = {
    "expected": "Expected redirect",
    "attention": "Redirect requiring attention",
    "loop": "Redirect loop",
    "cross_domain": "Cross-domain redirect",
    "excessive": "Excessive redirect chain",
}


@dataclass
class Hop:
    """One request in a redirect chain."""
    url: str
    status: Optional[int]          # None = the request itself failed
    location: Optional[str]        # absolute redirect target (only for redirects)
    elapsed: float = 0.0


@dataclass
class UrlResult:
    website: str
    url: str
    kind: str = "page"             # "page" or "asset"
    external: bool = False
    is_start: bool = False
    depth: int = 0
    status: Optional[int] = None
    final_url: str = ""
    chain: List[Hop] = field(default_factory=list)
    redirect_count: int = 0
    redirect_category: str = ""    # "", expected, attention, loop, cross_domain, excessive
    final_same_domain: bool = True
    error_type: str = ""
    error_detail: str = ""
    response_time: float = 0.0
    slow: bool = False
    content_type: str = ""
    canonical: str = ""
    notes: List[str] = field(default_factory=list)
    found_on: List[str] = field(default_factory=list)

    @property
    def is_error(self) -> bool:
        return bool(self.error_type)

    @property
    def has_redirect(self) -> bool:
        return self.redirect_count > 0

    @property
    def unexpected_redirect(self) -> bool:
        return self.redirect_category in UNEXPECTED_REDIRECTS

    @property
    def first_redirect_location(self) -> str:
        for h in self.chain:
            if h.location:
                return h.location
        return ""

    @property
    def label(self) -> str:
        """Human description of what kind of problem this is."""
        if not self.is_error:
            return ""
        if self.error_type in LOOP_ERRORS:
            return "Redirect loop"
        if self.is_start:
            if self.error_type in CONNECTION_ERRORS + SSL_DNS_ERRORS:
                return "Site unreachable"
            return "Homepage error"
        if self.kind == "asset":
            return "Broken image/asset"
        if self.external:
            return "Broken external link"
        if self.found_on:
            return "Broken internal link"
        return "Broken URL"

    def chain_lines(self) -> List[str]:
        lines = []
        for h in self.chain:
            st = str(h.status) if h.status is not None else "ERR"
            if h.location:
                lines.append("%s %s \u2192 %s" % (st, h.url, h.location))
            else:
                lines.append("%s %s" % (st, h.url))
        return lines


@dataclass
class SiteReport:
    website: str
    host: str = ""
    started: float = field(default_factory=time.time)
    duration: float = 0.0
    discovered: int = 0
    results: List[UrlResult] = field(default_factory=list)
    skipped: List[Dict[str, str]] = field(default_factory=list)
    skip_counts: Dict[str, int] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    # ---- filtered views -------------------------------------------------
    def errors(self) -> List[UrlResult]:
        return [r for r in self.results if r.is_error]

    def redirects(self) -> List[UrlResult]:
        return [r for r in self.results if r.has_redirect or r.error_type in LOOP_ERRORS]

    def unexpected_redirects(self) -> List[UrlResult]:
        return [r for r in self.results if r.unexpected_redirect]

    def slow_urls(self) -> List[UrlResult]:
        return sorted((r for r in self.results if r.slow),
                      key=lambda r: r.response_time, reverse=True)

    def broken_links(self) -> List[UrlResult]:
        return [r for r in self.results if r.is_error and not r.is_start and r.found_on]

    # ---- numbers --------------------------------------------------------
    def summary(self) -> Dict[str, int]:
        res = self.results

        def count(pred):
            return sum(1 for r in res if pred(r))

        return {
            "discovered": max(self.discovered, len(res)),
            "checked": len(res),
            "healthy": count(lambda r: not r.is_error),
            "e404": count(lambda r: r.error_type == "http_404"),
            "e4xx": count(lambda r: r.error_type == "http_4xx"),
            "e500": count(lambda r: r.error_type == "http_500"),
            "e5xx": count(lambda r: r.error_type == "http_5xx"),
            "other_errors": count(lambda r: r.error_type in OTHER_ERRORS),
            "broken_links": len(self.broken_links()),
            "redirects": count(lambda r: r.redirect_count > 0),
            "redirect_chains": count(lambda r: r.redirect_count >= 2),
            "redirect_loops": count(lambda r: r.error_type in LOOP_ERRORS),
            "cross_domain": count(lambda r: r.redirect_category == "cross_domain"),
            "redirect_attention": count(lambda r: r.unexpected_redirect),
            "slow": count(lambda r: r.slow),
            "connection_failures": count(lambda r: r.error_type in CONNECTION_ERRORS),
            "ssl_dns_failures": count(lambda r: r.error_type in SSL_DNS_ERRORS),
            "errors": count(lambda r: r.is_error),
        }

    @property
    def status(self) -> str:
        """DOWN / ERRORS / WARNINGS / OK for the site as a whole."""
        start = next((r for r in self.results if r.is_start), None)
        if start is None or start.is_error:
            return "DOWN"
        if any(r.is_error for r in self.results):
            return "ERRORS"
        if any(r.unexpected_redirect or r.slow for r in self.results):
            return "WARNINGS"
        return "OK"

    @classmethod
    def from_crash(cls, website: str, exc: Exception) -> "SiteReport":
        rep = cls(website=website)
        rep.results.append(UrlResult(
            website=website, url=website, is_start=True, error_type="other_error",
            error_detail="Crawler crashed: %s: %s" % (type(exc).__name__, exc)))
        return rep

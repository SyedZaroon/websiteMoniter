"""URL helpers: normalisation, de-duplication keys and host comparison."""
from __future__ import annotations

import os
from typing import Iterable, Optional
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
import re

# Links ending in these are checked as "assets" (HEAD request) and never crawled.
NON_HTML_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".svg", ".ico", ".bmp", ".avif",
    ".pdf", ".zip", ".gz", ".tar", ".rar", ".7z", ".dmg", ".exe", ".apk",
    ".mp3", ".mp4", ".mov", ".avi", ".webm", ".wav",
    ".css", ".js", ".mjs", ".json", ".xml", ".txt", ".csv",
    ".xls", ".xlsx", ".doc", ".docx", ".ppt", ".pptx",
    ".woff", ".woff2", ".ttf", ".eot", ".otf", ".map",
}

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")


def ensure_scheme(value: str) -> str:
    """'example.com' -> 'https://example.com'."""
    value = value.strip()
    if not _SCHEME_RE.match(value):
        value = "https://" + value
    return value


def host_of(url: str) -> str:
    try:
        return (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def bare_host(host: str) -> str:
    """Host without a leading 'www.' so www/non-www count as the same site."""
    host = host.lower()
    return host[4:] if host.startswith("www.") else host


def path_extension(url: str) -> str:
    try:
        return os.path.splitext(urlsplit(url).path)[1].lower()
    except ValueError:
        return ""


def _param_ignored(name: str, patterns: Iterable[str]) -> bool:
    name = name.lower()
    for p in patterns:
        p = p.lower()
        if p.endswith("*"):
            if name.startswith(p[:-1]):
                return True
        elif name == p:
            return True
    return False


def clean_url(url: str, ignore_query: bool = False,
              ignored_params: Iterable[str] = ()) -> str:
    """Return a tidy, fetchable URL (or '' if it is not a valid http(s) URL).

    - lower-cases scheme and host, drops default ports, user-info and #fragment
    - removes tracking parameters (or the whole query if ignore_query)
    - sorts the remaining query parameters
    """
    try:
        parts = urlsplit(url.strip())
        scheme = parts.scheme.lower()
        host = (parts.hostname or "").lower()
        port = parts.port
    except ValueError:
        return ""
    if scheme not in ("http", "https") or not host:
        return ""
    if ":" in host:  # IPv6 literal
        host = "[%s]" % host
    netloc = host
    if port and not ((scheme == "http" and port == 80) or (scheme == "https" and port == 443)):
        netloc += ":%d" % port
    path = parts.path or "/"
    query = ""
    if parts.query and not ignore_query:
        pairs = parse_qsl(parts.query, keep_blank_values=True)
        kept = sorted((k, v) for k, v in pairs if not _param_ignored(k, ignored_params))
        query = urlencode(kept)
    return urlunsplit((scheme, netloc, path, query, ""))


def dedupe_key(url: str) -> str:
    """Key used to decide whether two URLs are 'the same page'.

    Identical to the cleaned URL except that a trailing slash is ignored, so
    /about and /about/ are only checked once.
    """
    parts = urlsplit(url)
    path = parts.path
    if len(path) > 1:
        path = path.rstrip("/") or "/"
    return urlunsplit((parts.scheme, parts.netloc, path, parts.query, ""))

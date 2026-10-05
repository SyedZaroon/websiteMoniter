"""Configuration defaults, loading and validation."""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

BASE_DIR = Path(__file__).resolve().parent


class ConfigError(Exception):
    """Raised for any problem with config.json / websites.json."""


DEFAULTS: Dict[str, Any] = {
    # --- which sites -----------------------------------------------------
    "websites_file": "websites.json",
    "websites": [],
    # --- crawl size ------------------------------------------------------
    "max_pages": 200,              # internal HTML pages per website
    "max_depth": 8,                # link depth from the homepage
    "max_assets": 200,             # images/css/js/files checked per website
    "max_external_links": 100,     # only used if check_external_links is true
    "site_time_limit": 1800,       # seconds per website (0 = unlimited)
    # --- politeness / speed ---------------------------------------------
    "timeout": 20,
    "retries": 2,
    "retry_backoff": 1.0,
    "concurrency": 4,              # workers per website
    "max_concurrent_sites": 6,     # websites crawled at the same time
    "per_host_concurrency": 3,     # simultaneous requests to one host
    "delay": 0.25,                 # minimum seconds between request starts per host
    "max_connections": 100,
    "trust_env": True,             # honour HTTP(S)_PROXY environment variables
    "user_agent": "WebsiteHealthMonitor/1.0 (+personal uptime checker)",
    "respect_robots": True,
    # --- URL handling ----------------------------------------------------
    "ignore_query": False,         # true = strip every ?query before checking
    "ignored_query_params": ["utm_*", "fbclid", "gclid", "msclkid", "mc_cid", "mc_eid", "_ga"],
    "max_query_variants": 5,       # distinct ?queries crawled per path
    "allowed_domains": [],         # extra hosts treated as "internal"
    "include_subdomains": False,
    "allow_patterns": [],          # regex; if set, only matching pages are crawled
    "ignore_patterns": ["/wp-admin", "/wp-login\\.php", "/cdn-cgi/", "logout"],
    # --- what to check ---------------------------------------------------
    "check_assets": True,
    "check_external_images": True,
    "check_external_links": False,
    # --- thresholds / redirect policy -----------------------------------
    "slow_threshold": 5.0,         # seconds
    "max_redirect_chain": 2,       # more redirects than this = "excessive chain"
    "max_redirects": 10,           # hard stop when following redirects
    "max_body_bytes": 3000000,
    "expected_redirects": {
        "http_to_https": True,
        "www_redirects": True,     # example.com <-> www.example.com
        "trailing_slash": True,    # /about <-> /about/
        "allowed_cross_domains": [],
        "rules": [],               # [{"from": "regex", "to": "regex (optional)"}]
    },
    # --- output ----------------------------------------------------------
    "reports_dir": "reports",
    "terminal_max_items": 15,      # problems printed per section per website
}

# Settings that may be overridden for a single website in websites.json
SITE_OVERRIDABLE = {
    "max_pages", "max_depth", "max_assets", "max_external_links", "site_time_limit",
    "ignore_query", "ignored_query_params", "max_query_variants", "allowed_domains",
    "include_subdomains", "allow_patterns", "ignore_patterns", "check_assets",
    "check_external_images", "check_external_links", "slow_threshold",
    "max_redirect_chain", "expected_redirects", "respect_robots",
}


def resolve_path(p: Any) -> Path:
    path = Path(str(p)).expanduser()
    if path.is_absolute():
        return path
    cwd_candidate = Path.cwd() / path
    if cwd_candidate.exists():
        return cwd_candidate
    return BASE_DIR / path


def _read_json(path: Path) -> Any:
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise ConfigError("File not found: %s" % path)
    except json.JSONDecodeError as exc:
        raise ConfigError("%s is not valid JSON: %s" % (path, exc))


def _merge_expected(base: Dict[str, Any], extra: Any) -> Dict[str, Any]:
    if not isinstance(extra, dict):
        raise ConfigError('"expected_redirects" must be an object')
    unknown = set(extra) - set(DEFAULTS["expected_redirects"])
    if unknown:
        raise ConfigError("Unknown expected_redirects setting(s): %s" % ", ".join(sorted(unknown)))
    merged = dict(base)
    merged.update(extra)
    return merged


def _validate(cfg: Dict[str, Any]) -> None:
    def num(key, minimum=0.0, allow_zero=True):
        v = cfg[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ConfigError('"%s" must be a number' % key)
        if v < minimum or (not allow_zero and v == 0):
            raise ConfigError('"%s" must be %s %s' % (key, ">" if not allow_zero else ">=", minimum))

    for key in ("max_pages", "max_depth", "max_assets", "max_external_links", "timeout",
                "concurrency", "max_concurrent_sites", "per_host_concurrency",
                "max_connections", "max_redirects", "max_body_bytes"):
        num(key, 1)
    for key in ("retries", "retry_backoff", "delay", "site_time_limit", "max_query_variants",
                "slow_threshold", "max_redirect_chain", "terminal_max_items"):
        num(key, 0)
    for key in ("respect_robots", "ignore_query", "include_subdomains", "check_assets",
                "check_external_images", "check_external_links", "trust_env"):
        if not isinstance(cfg[key], bool):
            raise ConfigError('"%s" must be true or false' % key)
    for key in ("allowed_domains", "allow_patterns", "ignore_patterns", "ignored_query_params"):
        if not isinstance(cfg[key], list) or not all(isinstance(x, str) for x in cfg[key]):
            raise ConfigError('"%s" must be a list of strings' % key)
    for key in ("allow_patterns", "ignore_patterns"):
        for pattern in cfg[key]:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError('Invalid regex in "%s": %r (%s)' % (key, pattern, exc))
    exp = cfg["expected_redirects"]
    if not isinstance(exp.get("allowed_cross_domains"), list):
        raise ConfigError('"expected_redirects.allowed_cross_domains" must be a list')
    rules = exp.get("rules")
    if not isinstance(rules, list):
        raise ConfigError('"expected_redirects.rules" must be a list')
    normalised = []
    for rule in rules:
        if isinstance(rule, str):
            rule = {"from": rule}
        if not isinstance(rule, dict) or "from" not in rule:
            raise ConfigError('Each expected_redirects rule needs a "from" regex')
        for k in ("from", "to"):
            if rule.get(k):
                try:
                    re.compile(rule[k])
                except re.error as exc:
                    raise ConfigError("Invalid regex in expected_redirects rule: %r (%s)" % (rule[k], exc))
        normalised.append(rule)
    exp["rules"] = normalised
    if not isinstance(cfg["user_agent"], str) or not cfg["user_agent"].strip():
        raise ConfigError('"user_agent" must be a non-empty string')


def load_config(path: Optional[Any] = None) -> Dict[str, Any]:
    user: Dict[str, Any] = {}
    if path:
        user = _read_json(resolve_path(path))
        if not isinstance(user, dict):
            raise ConfigError("config.json must contain a JSON object")
    unknown = sorted(k for k in user if k not in DEFAULTS and not k.startswith("_"))
    if unknown:
        raise ConfigError("Unknown setting(s) in config: %s" % ", ".join(unknown))
    cfg = copy.deepcopy(DEFAULTS)
    for key, value in user.items():
        if key.startswith("_"):
            continue
        if key == "expected_redirects":
            cfg[key] = _merge_expected(cfg[key], value)
        else:
            cfg[key] = value
    _validate(cfg)
    return cfg


def load_websites(cfg: Dict[str, Any], path: Optional[Any] = None) -> List[Tuple[str, Dict[str, Any]]]:
    """Return [(url, per-site overrides)] from config 'websites' + websites file."""
    entries: List[Any] = list(cfg["websites"])
    file_path = resolve_path(path or cfg["websites_file"])
    if file_path.exists():
        data = _read_json(file_path)
        if isinstance(data, dict):
            data = data.get("websites", [])
        if not isinstance(data, list):
            raise ConfigError("%s must contain a JSON list of websites" % file_path)
        entries += data
    elif path:
        raise ConfigError("Websites file not found: %s" % file_path)

    sites: List[Tuple[str, Dict[str, Any]]] = []
    seen = set()
    for entry in entries:
        overrides: Dict[str, Any] = {}
        if isinstance(entry, str):
            url = entry.strip()
        elif isinstance(entry, dict) and isinstance(entry.get("url"), str):
            url = entry["url"].strip()
            overrides = {k: v for k, v in entry.items() if k != "url"}
            bad = sorted(set(overrides) - SITE_OVERRIDABLE)
            if bad:
                raise ConfigError("Setting(s) %s cannot be set per website (%s)" % (", ".join(bad), url))
        else:
            raise ConfigError("Each website must be a string or an object with a \"url\": %r" % (entry,))
        if not url or url.startswith("#"):
            continue
        key = re.sub(r"^https?://", "", url.lower()).rstrip("/")
        if key in seen:
            continue
        seen.add(key)
        sites.append((url, overrides))
    if not sites:
        raise ConfigError("No websites configured. Add some to websites.json.")
    return sites


def site_config(cfg: Dict[str, Any], overrides: Dict[str, Any]) -> Dict[str, Any]:
    """Global config + per-website overrides, validated."""
    merged = copy.deepcopy(cfg)
    for key, value in overrides.items():
        if key == "expected_redirects":
            merged[key] = _merge_expected(merged[key], value)
        else:
            merged[key] = value
    _validate(merged)
    return merged

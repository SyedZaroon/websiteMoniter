#!/usr/bin/env python3
"""Website health monitor - crawl a list of sites and report broken things.

Usage:
    python main.py                       # all sites in websites.json
    python main.py --site example.com    # just one site (can be repeated)
    python main.py --max-pages 50        # smaller crawl, e.g. for a quick test
    python main.py --open                # open the HTML report when finished
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import webbrowser
from datetime import datetime
from typing import Any, Dict, List, Tuple

from checker import HostThrottle, make_client
from config import BASE_DIR, ConfigError, load_config, load_websites, site_config
from crawler import SiteCrawler
from models import SiteReport
import reporter


def log(message: str) -> None:
    print(message, flush=True)


async def monitor(cfg: Dict[str, Any], sites: List[Tuple[str, Dict[str, Any]]]) -> List[SiteReport]:
    async with make_client(cfg) as client:
        throttle = HostThrottle(cfg["delay"], cfg["per_host_concurrency"])
        gate = asyncio.Semaphore(cfg["max_concurrent_sites"])

        async def one(url: str, overrides: Dict[str, Any]) -> SiteReport:
            async with gate:
                log("\u25b6 Checking %s" % url)
                try:
                    crawler = SiteCrawler(url, site_config(cfg, overrides), client, throttle, log)
                    report = await crawler.run()
                except Exception as exc:       # one broken site must not stop the others
                    report = SiteReport.from_crash(url, exc)
                s = report.summary()
                log("\u2714 %s: %d URLs checked, %d errors, %d redirects to review, %d slow  [%s]"
                    % (report.host or url, s["checked"], s["errors"], s["redirect_attention"],
                       s["slow"], report.status))
                return report

        return list(await asyncio.gather(*(one(u, o) for u, o in sites)))


def parse_args(argv: List[str]) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="Crawl websites and report errors, redirects and slow pages.")
    ap.add_argument("--config", default="config.json", help="settings file (default: config.json)")
    ap.add_argument("--websites", default=None, help="websites list (default: websites_file from config)")
    ap.add_argument("--site", action="append", default=[],
                    help="check only this site (repeatable); it does not need to be in websites.json")
    ap.add_argument("--max-pages", type=int, default=None, help="override max_pages for every site")
    ap.add_argument("--open", action="store_true", help="open the HTML report in your browser when done")
    ap.add_argument("--exit-zero", action="store_true",
                    help="always exit with status 0 (handy for schedulers)")
    return ap.parse_args(argv)


def main(argv: List[str] = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        cfg = load_config(args.config)
        if args.max_pages:
            cfg["max_pages"] = args.max_pages
        if args.site:
            sites = [(s, {}) for s in args.site]
        else:
            sites = load_websites(cfg, args.websites)
        for url, overrides in sites:      # fail early on bad per-site overrides
            site_config(cfg, overrides)
    except ConfigError as exc:
        print("Configuration error: %s" % exc, file=sys.stderr)
        return 2

    started = datetime.now()
    print("Website Health Monitor - %d site(s), up to %d pages each, %d sites at a time\n"
          % (len(sites), cfg["max_pages"], cfg["max_concurrent_sites"]))
    try:
        reports = asyncio.run(monitor(cfg, sites))
    except KeyboardInterrupt:
        print("\nInterrupted - no report written.")
        return 130
    finished = datetime.now()

    paths = reporter.write_reports(reports, cfg, started, finished, BASE_DIR)
    reporter.print_terminal(reports, cfg, paths, started, finished)
    if args.open:
        webbrowser.open(paths["html"].resolve().as_uri())

    has_errors = any(r.errors() for r in reports)
    return 0 if (args.exit_zero or not has_errors) else 1


if __name__ == "__main__":
    sys.exit(main())

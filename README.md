# Website Health Monitor

Crawls a list of websites you own and reports broken pages, bad redirects, errors and slow responses.
Runs locally on your Mac. No paid services.

```
website-monitor/
├── main.py              entry point (CLI)
├── alerts.py            email alerts for outages and cross-domain redirects
├── crawler.py           queue-based crawler: link discovery, robots.txt, limits
├── checker.py           checks one URL: redirect chain, errors, retries, throttling
├── reporter.py          terminal summary, HTML report, CSV
├── config.py            defaults + validation
├── models.py, urlutils.py
├── config.json          your settings
├── websites.json        your list of sites
├── run.command          double-click launcher for macOS
├── install_daily.sh     install hourly schedule (legacy filename)
├── uninstall_daily.sh   remove the scheduled job
├── test_monitor.py      automated tests (local test server)
└── reports/             output (YYYY-MM-DD-HH-MM-report.html / .csv, latest-report.html)
```

## How it works

1. For each website the crawler starts at the homepage and keeps a queue of internal URLs (breadth-first).
2. Every URL is checked once. URLs are normalised first (fragments removed, tracking parameters like `utm_*` removed,
   query parameters sorted, `/about` = `/about/`), so duplicates are skipped.
3. HTML pages are parsed for `<a href>`, images (`src`, `srcset`, `data-src`), stylesheets, scripts and
   canonical URLs. `#anchors`, `mailto:`, `tel:` and `javascript:` links are ignored.
4. Internal pages are crawled further. Images/CSS/JS/PDF files are only *checked* (a light `HEAD` request, falling back to `GET`).
5. External links are not crawled. External *images* are checked; external *links* only if you set `check_external_links`.
6. Redirects are followed one hop at a time so the full chain is recorded, loops are detected, and every redirect is
   categorised (see below).
7. Requests are throttled per host (minimum gap + max simultaneous requests), `robots.txt` is honoured (including
   `Crawl-delay`), failed requests are retried with back-off, and nothing tries to bypass authentication, WAFs, CAPTCHAs or rate limits.
   The homepage you list is always checked even if robots.txt disallows it.

### Redirect categories

| Category | Meaning |
|---|---|
| Expected redirect | Every hop is an allowed kind: HTTP→HTTPS, www↔non-www, trailing slash, or matches one of your `rules` |
| Redirect requiring attention | Any other redirect (e.g. `/old-page` → `/new-page`) |
| Redirect loop | A URL repeats in the chain (or more than `max_redirects` hops) |
| Cross-domain redirect | Final URL is on a different domain (unless listed in `allowed_cross_domains`) |
| Excessive redirect chain | More than `max_redirect_chain` redirects (default 2) |

Redirects are never counted as errors unless they loop, break, or end in an error status.

### Problem types

404 · 500 · other 4xx · other 5xx · timeout · connection error · DNS error · SSL error · redirect loop · bad redirect.
Each failing URL also says *what it is*: broken internal link, broken image/asset, broken external link, or homepage/site unreachable,
plus the pages it was found on. A site's status is `OK`, `WARNINGS` (redirects to review / slow pages), `ERRORS` or `DOWN` (homepage failed).

## Installation (macOS)

You need Python 3.9 or newer (`python3 --version`). If missing: `xcode-select --install` or install from python.org.

```bash
cd website-monitor
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

## Running

```bash
source .venv/bin/activate        # once per terminal window
python main.py
```

Useful options:

```bash
python main.py --open                    # open the HTML report when finished
python main.py --site example.com        # check just one site (repeatable)
python main.py --max-pages 50            # quick, smaller crawl
python main.py --exit-zero               # always exit 0 (used by the scheduler)
```

Exit code: `0` = no errors, `1` = at least one error found, `2` = configuration problem.

**Start small:** run `python main.py --site yourshop.com --max-pages 30` first to see the output, then run everything.

### Double-click launcher

`run.command` is already in the project. Make it executable once:

```bash
chmod +x run.command
```

Then double-click it in Finder. On first run it creates `.venv` and installs the requirements, then it runs the checker,
opens the HTML report and waits for a key press before closing. If macOS says it can't verify the developer,
right-click the file → **Open** → **Open** (only needed the first time).

## Hourly automation (GitHub Actions)

The workflow in `.github/workflows/hourly-monitor.yml` runs hourly on GitHub's
servers, even while your Mac is off. GitHub may delay scheduled workflows during
busy periods. Commit and push the workflow to the repository's default branch;
the schedule will then appear under **Actions**. To enable email alerts, add
these repository Actions secrets under
**Settings → Secrets and variables → Actions → New repository secret**:

| Secret name | Value |
|---|---|
| `WEBSITE_MONITOR_EMAIL_TO` | `zaroonalichishti@gmail.com` |
| `WEBSITE_MONITOR_SMTP_USERNAME` | Your Gmail address |
| `WEBSITE_MONITOR_SMTP_PASSWORD` | A Gmail [App Password](https://support.google.com/accounts/answer/185833), not your regular account password |

The SMTP host defaults to Gmail on port 587, and the sender defaults to the
SMTP username. A different provider can be configured by adding the
`WEBSITE_MONITOR_SMTP_HOST`, `WEBSITE_MONITOR_SMTP_PORT`, and
`WEBSITE_MONITOR_EMAIL_FROM` secrets and mapping them in the workflow.

The workflow emails when a monitored homepage is down or any checked URL
redirects to a different domain. Expected redirects such as HTTP→HTTPS do not
trigger an alert. Use the **Actions** tab to run it manually with
**Hourly website monitor → Run workflow**. Reports from hosted runs are in the
Actions run logs; generated files are not copied back into the repository.

For local-only scheduling instead, `./install_daily.sh` installs a macOS
`launchd` job that runs hourly while your Mac is on; it cannot run while the Mac
is shut down. The local schedule can be removed with `./uninstall_daily.sh`.
For local email configuration, copy `.env.example` to `.env` and keep the
file private; `.env` is excluded from version control.

## Configuration

`websites.json` is a list. Entries can be plain URLs/domains or objects with per-site overrides:

```json
[
  "https://shop-one.com",
  "shop-two.com",
  { "url": "https://big-shop.com", "max_pages": 800, "ignore_patterns": ["/search", "/cart"] },
  "# lines starting with # are ignored"
]
```

Per-site overrides can use: `max_pages, max_depth, max_assets, max_external_links, site_time_limit, ignore_query,
ignored_query_params, max_query_variants, allowed_domains, include_subdomains, allow_patterns, ignore_patterns,
check_assets, check_external_images, check_external_links, slow_threshold, max_redirect_chain, expected_redirects, respect_robots`.

`config.json` (every key optional; unknown keys are rejected so typos don't go unnoticed):

| Setting | Default | Meaning |
|---|---|---|
| `max_pages` | 200 | Internal pages checked per website |
| `max_depth` | 8 | Link depth from the homepage |
| `max_assets` | 200 | Images/CSS/JS/files checked per website |
| `site_time_limit` | 1800 | Seconds per website (0 = unlimited) |
| `timeout` | 20 | Request timeout (seconds) |
| `retries` | 2 | Retries for timeouts, connection errors and 429/502/503/504 |
| `retry_backoff` | 1.0 | Seconds; doubles each retry |
| `concurrency` | 4 | Workers per website |
| `max_concurrent_sites` | 6 | Websites crawled at once |
| `per_host_concurrency` | 3 | Simultaneous requests to one host |
| `delay` | 0.25 | Minimum seconds between request starts to one host |
| `user_agent` | WebsiteHealthMonitor/1.0 … | Sent with every request (put a contact email in it if you like) |
| `respect_robots` | true | Obey robots.txt for crawled pages |
| `ignore_query` | false | Strip all `?query` strings |
| `ignored_query_params` | utm_*, fbclid, gclid … | Tracking params removed (`*` = prefix) |
| `max_query_variants` | 5 | Distinct `?queries` crawled for one path (stops faceted-filter explosions) |
| `allowed_domains` | [] | Extra hosts treated as part of the site |
| `include_subdomains` | false | Treat `*.example.com` as internal |
| `allow_patterns` | [] | Regexes; if set, only matching pages are crawled |
| `ignore_patterns` | wp-admin, logout … | Regexes; matching URLs are skipped |
| `check_assets` | true | Check internal images/CSS/JS/files |
| `check_external_images` | true | Check images hosted on other domains (CDNs) |
| `check_external_links` | false | Check (not crawl) links to other sites |
| `slow_threshold` | 5.0 | Seconds before a URL is flagged slow |
| `max_redirect_chain` | 2 | Redirects allowed before "excessive chain" |
| `max_redirects` | 10 | Hard stop when following redirects |
| `expected_redirects` | see below | What counts as a normal redirect |
| `trust_env` | true | Use proxy environment variables if set |
| `reports_dir` | reports | Output folder |

`expected_redirects`:

```json
"expected_redirects": {
  "http_to_https": true,
  "www_redirects": true,
  "trailing_slash": true,
  "allowed_cross_domains": ["payments.example-provider.com"],
  "rules": [ { "from": "/old-blog/", "to": "/blog/" } ]
}
```

`rules` are regexes matched against each redirect hop (`from` = the URL redirected, `to` = the target; `to` is optional).

### Tuning for 20+ sites

Defaults are roughly 4 requests/second per site and 6 sites at once, so 20 sites at 200 pages take around 5–10 minutes.
If a site is slow or fragile, raise `delay` or lower `per_host_concurrency` for it. If a site is larger than `max_pages`,
the report says how many pages were found but not checked.

## Reports

* **Terminal**: per-site summary, then each problem (ERROR / REDIRECT / SLOW blocks).
* **HTML** `reports/YYYY-MM-DD-HH-MM-report.html`: overall summary, website table, errors, redirects (expected ones collapsed),
  slow URLs, broken links with "linked from" pages, and a searchable detailed table per site.
* **CSV** `reports/YYYY-MM-DD-HH-MM-report.csv`: one row per checked URL, opens in Numbers/Excel.
* `reports/latest-report.html` is always a copy of the newest HTML report.

## Tests

```bash
python -m unittest test_monitor -v
```

A local test server simulates 200, 301, multi-hop redirects, redirect loops, cross-domain redirects, 403/404/500, timeouts, 503-then-200
(retry), HEAD rejection, robots.txt, broken internal links and images. DNS failure uses a guaranteed-invalid hostname and SSL failure uses a
self-signed HTTPS server (needs `openssl`, which macOS has).

## Troubleshooting

* **Every HTTPS site shows SSL errors**: update certifi (`pip install -U certifi httpx`). Python from python.org may also need
  `/Applications/Python 3.x/Install Certificates.command` run once.
* **Many 403/429 responses**: the site is rate-limiting you. Increase `delay`, lower concurrency, and keep `respect_robots` on.
  The tool will not try to get around blocks.
* **Single-page JavaScript sites**: links are read from the HTML only; pages whose links are generated by JavaScript can't be discovered.
* **A page is "slow" only sometimes**: slow means a single response over `slow_threshold`; re-run to confirm.

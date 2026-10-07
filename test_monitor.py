"""Tests for the website monitor.

Run from the project folder:

    python -m unittest discover -s tests -v

A small local web server simulates every situation: 200, 301, redirect chains,
redirect loops, cross-domain redirects, 403/404/500, timeouts, flaky servers,
broken internal links/images, robots.txt, ... DNS and SSL failures use a
guaranteed-invalid hostname and a self-signed HTTPS server.
"""
import asyncio
import os
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checker import Checker, HostThrottle, categorize_redirect, hop_is_expected, make_client  # noqa: E402
from alerts import AlertError, send_alert  # noqa: E402
from config import load_config, load_websites, site_config, ConfigError  # noqa: E402
from crawler import SiteCrawler, extract_page_info  # noqa: E402
from models import SiteReport, UrlResult  # noqa: E402
import reporter  # noqa: E402
from urlutils import clean_url, dedupe_key  # noqa: E402

FLAKY_HITS = {"n": 0}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send(self, code, body=b"", ctype="text/html", headers=None, head=False):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def _handle(self, head):
        port = self.server.server_port
        path = self.path
        me = "http://127.0.0.1:%d" % port
        send = lambda *a, **k: self._send(*a, head=head, **k)  # noqa: E731

        if path == "/":
            html = """<html><head><title>Home</title>
              <link rel="canonical" href="%(me)s/"><link rel="stylesheet" href="/style.css"></head><body>
              <a href="/ok">ok</a> <a href="/missing">missing</a> <a href="/boom">boom</a>
              <a href="/forbidden">forbidden</a> <a href="/old">old</a> <a href="/chain1">chain</a>
              <a href="/loop-a">loop</a> <a href="/cross">cross</a> <a href="/dir">dir</a>
              <a href="/p?utm_source=x">p1</a> <a href="/p">p2</a> <a href="/ok#frag">frag</a>
              <a href="mailto:a@b.c">m</a> <a href="tel:123">t</a> <a href="javascript:void(0)">j</a>
              <a href="#top">top</a> <a href="/private/secret">private</a>
              <a href="/files/report.pdf">pdf</a> <a href="http://other-site.invalid/x">external</a>
              <img src="/img/ok.png"><img src="/img/missing.png">
              </body></html>""" % {"me": me}
            return send(200, html.encode())
        if path in ("/ok", "/new", "/p", "/dir/", "/landing", "/p?utm_source=x") or path.startswith("/p?"):
            return send(200, b'<html><body>page <a href="/">home</a></body></html>')
        if path == "/style.css":
            return send(200, b"body{}", "text/css")
        if path == "/img/ok.png":
            return send(200, b"\x89PNG", "image/png")
        if path == "/files/report.pdf":
            if head:                      # some servers reject HEAD -> checker must fall back to GET
                return send(405)
            return send(200, b"%PDF", "application/pdf")
        if path == "/missing" or path == "/img/missing.png":
            return send(404, b"nope")
        if path == "/boom":
            return send(500, b"error")
        if path == "/forbidden":
            return send(403, b"no")
        if path == "/old":
            return send(301, headers={"Location": "/new"})
        if path == "/dir":
            return send(301, headers={"Location": "/dir/"})
        if path == "/chain1":
            return send(302, headers={"Location": "/chain2"})
        if path == "/chain2":
            return send(307, headers={"Location": "/chain3"})
        if path == "/chain3":
            return send(308, headers={"Location": "/ok"})
        if path == "/loop-a":
            return send(302, headers={"Location": "/loop-b"})
        if path == "/loop-b":
            return send(302, headers={"Location": "/loop-a"})
        if path == "/cross":
            return send(301, headers={"Location": "http://localhost:%d/landing" % port})
        if path == "/timeout":
            time.sleep(4)
            return send(200, b"late")
        if path == "/slowpage":
            time.sleep(0.7)
            return send(200, b"<html>slow</html>")
        if path == "/flaky":
            FLAKY_HITS["n"] += 1
            return send(503 if FLAKY_HITS["n"] == 1 else 200, b"<html>ok</html>")
        if path == "/robots.txt":
            return send(200, b"User-agent: *\nDisallow: /private\n", "text/plain")
        if path.startswith("/private"):
            return send(200, b"<html>secret</html>")
        return send(404, b"unknown")

    def do_GET(self):
        self._handle(False)

    def do_HEAD(self):
        self._handle(True)


class QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass


def start_server(ssl_files=None):
    httpd = QuietServer(("127.0.0.1", 0), Handler)
    if ssl_files:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(*ssl_files)
        httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def test_cfg(**overrides):
    cfg = load_config(None)
    cfg.update(delay=0.0, timeout=1.5, retries=0, retry_backoff=0.0, trust_env=False,
               slow_threshold=0.5, per_host_concurrency=4)
    cfg.update(overrides)
    return cfg


class ServerTestCase(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = start_server()
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_port

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()

    async def asyncSetUp(self):
        self.cfg = test_cfg()
        self.client = make_client(self.cfg)

    async def asyncTearDown(self):
        await self.client.aclose()

    def checker(self, cfg=None):
        cfg = cfg or self.cfg
        return Checker(self.client, HostThrottle(0, 4), cfg)

    async def check(self, path, cfg=None, **kw):
        res, body = await self.checker(cfg).check("test", self.base + path, **kw)
        return res


class StatusTests(ServerTestCase):
    async def test_200(self):
        r = await self.check("/ok")
        self.assertEqual((r.status, r.error_type, r.redirect_count), (200, "", 0))
        self.assertFalse(r.is_error)

    async def test_404(self):
        r = await self.check("/missing")
        self.assertEqual((r.status, r.error_type), (404, "http_404"))

    async def test_500(self):
        r = await self.check("/boom")
        self.assertEqual((r.status, r.error_type), (500, "http_500"))

    async def test_403_is_other_4xx(self):
        r = await self.check("/forbidden")
        self.assertEqual((r.status, r.error_type), (403, "http_4xx"))

    async def test_timeout(self):
        r = await self.check("/timeout")
        self.assertEqual(r.error_type, "timeout")
        self.assertIsNone(r.status)

    async def test_connection_refused(self):
        sock = __import__("socket").socket()
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
        sock.close()                      # nothing is listening on this port now
        res, _ = await self.checker().check("t", "http://127.0.0.1:%d/" % port)
        self.assertEqual(res.error_type, "connection_error")

    async def test_dns_failure(self):
        res, _ = await self.checker().check("t", "http://this-host-does-not-exist.invalid/")
        self.assertEqual(res.error_type, "dns_error", res.error_detail)

    async def test_slow_page_flagged(self):
        r = await self.check("/slowpage")
        self.assertEqual(r.status, 200)
        self.assertTrue(r.slow)
        self.assertGreaterEqual(r.response_time, 0.7)

    async def test_retry_on_503(self):
        FLAKY_HITS["n"] = 0
        r = await self.check("/flaky", cfg=test_cfg(retries=1))
        self.assertEqual(r.status, 200)
        self.assertEqual(FLAKY_HITS["n"], 2)

    async def test_no_retry_when_disabled(self):
        FLAKY_HITS["n"] = 0
        r = await self.check("/flaky", cfg=test_cfg(retries=0))
        self.assertEqual(r.error_type, "http_5xx")

    async def test_head_rejected_falls_back_to_get(self):
        r = await self.check("/files/report.pdf", kind="asset")
        self.assertEqual((r.status, r.error_type), (200, ""))


class RedirectTests(ServerTestCase):
    async def test_single_301(self):
        r = await self.check("/old")
        self.assertEqual([h.status for h in r.chain], [301, 200])
        self.assertEqual(r.redirect_count, 1)
        self.assertEqual(r.final_url, self.base + "/new")
        self.assertEqual(r.first_redirect_location, self.base + "/new")
        self.assertEqual(r.redirect_category, "attention")
        self.assertTrue(r.unexpected_redirect)
        self.assertTrue(r.final_same_domain)
        self.assertEqual(r.chain_lines()[0], "301 %s/old \u2192 %s/new" % (self.base, self.base))

    async def test_multiple_redirects_are_excessive(self):
        r = await self.check("/chain1")
        self.assertEqual([h.status for h in r.chain], [302, 307, 308, 200])
        self.assertEqual(r.redirect_count, 3)
        self.assertEqual(r.redirect_category, "excessive")
        self.assertEqual(r.final_url, self.base + "/ok")

    async def test_chain_allowed_when_threshold_raised(self):
        r = await self.check("/chain1", cfg=test_cfg(max_redirect_chain=5))
        self.assertEqual(r.redirect_category, "attention")

    async def test_redirect_loop(self):
        r = await self.check("/loop-a")
        self.assertEqual(r.error_type, "redirect_loop")
        self.assertEqual(r.redirect_category, "loop")
        self.assertIn("Redirect loop", r.error_detail)

    async def test_cross_domain(self):
        r = await self.check("/cross")
        self.assertEqual(r.redirect_category, "cross_domain")
        self.assertFalse(r.final_same_domain)
        self.assertEqual(r.status, 200)

    async def test_cross_domain_can_be_allowed(self):
        cfg = test_cfg()
        cfg["expected_redirects"]["allowed_cross_domains"] = ["localhost"]
        r = await self.check("/cross", cfg=cfg)
        self.assertNotEqual(r.redirect_category, "cross_domain")

    async def test_trailing_slash_redirect_is_expected(self):
        r = await self.check("/dir")
        self.assertEqual(r.redirect_category, "expected")
        self.assertFalse(r.unexpected_redirect)

    async def test_trailing_slash_not_expected_when_disabled(self):
        cfg = test_cfg()
        cfg["expected_redirects"]["trailing_slash"] = False
        r = await self.check("/dir", cfg=cfg)
        self.assertEqual(r.redirect_category, "attention")

    async def test_custom_rule_marks_redirect_expected(self):
        cfg = test_cfg()
        cfg["expected_redirects"]["rules"] = [{"from": "/old$", "to": "/new$"}]
        r = await self.check("/old", cfg=cfg)
        self.assertEqual(r.redirect_category, "expected")


class PureLogicTests(unittest.TestCase):
    EXP = {"http_to_https": True, "www_redirects": True, "trailing_slash": True,
           "allowed_cross_domains": [], "rules": []}

    def test_http_to_https_expected(self):
        self.assertTrue(hop_is_expected("http://a.com/x", "https://a.com/x", self.EXP))
        self.assertEqual(categorize_redirect(
            ["http://a.com/", "https://a.com/"], False, lambda h: h == "a.com", 2, self.EXP), "expected")

    def test_http_to_https_to_www_expected(self):
        urls = ["http://a.com/", "https://a.com/", "https://www.a.com/"]
        self.assertEqual(categorize_redirect(urls, False, lambda h: h.endswith("a.com"), 2, self.EXP), "expected")

    def test_https_to_http_is_not_expected(self):
        self.assertFalse(hop_is_expected("https://a.com/x", "http://a.com/x", self.EXP))

    def test_different_path_not_expected(self):
        self.assertFalse(hop_is_expected("https://a.com/old", "https://a.com/new", self.EXP))

    def test_http_to_https_can_be_disabled(self):
        exp = dict(self.EXP, http_to_https=False)
        self.assertFalse(hop_is_expected("http://a.com/x", "https://a.com/x", exp))

    def test_url_normalisation(self):
        c = lambda u: clean_url(u, False, ["utm_*", "fbclid"])  # noqa: E731
        self.assertEqual(c("HTTPS://Example.COM:443/a#frag"), "https://example.com/a")
        self.assertEqual(c("https://example.com"), "https://example.com/")
        self.assertEqual(c("https://example.com/a?b=2&utm_source=x&a=1"), "https://example.com/a?a=1&b=2")
        self.assertEqual(c("mailto:x@y.z"), "")
        self.assertEqual(dedupe_key("https://e.com/about/"), dedupe_key("https://e.com/about"))
        self.assertEqual(clean_url("https://e.com/a?x=1", True, []), "https://e.com/a")

    def test_link_extraction(self):
        html = b"""<base href="https://e.com/dir/"><a href="page">r</a><a href="/abs">a</a>
          <a href="#x">anchor</a><a href="mailto:a@b.c">m</a><a href="tel:1">t</a><a href="javascript:void(0)">j</a>
          <img src="i.png" srcset="i1.png 1x, i2.png 2x"><script src="/s.js"></script>
          <link rel="stylesheet" href="c.css"><link rel="canonical" href="/canon">"""
        info = extract_page_info(html, "https://e.com/")
        self.assertEqual(info.links, ["https://e.com/dir/page", "https://e.com/abs"])
        assets = sorted(u for _, u in info.assets)
        self.assertEqual(assets, sorted(["https://e.com/dir/i.png", "https://e.com/dir/i1.png",
                                         "https://e.com/dir/i2.png", "https://e.com/s.js",
                                         "https://e.com/dir/c.css"]))
        self.assertEqual(info.canonical, "https://e.com/canon")

    def test_config_validation(self):
        with self.assertRaises(ConfigError):
            site_config(load_config(None), {"max_pages": 0})
        with self.assertRaises(ConfigError):
            site_config(load_config(None), {"ignore_patterns": ["("]})
        cfg = load_config(Path(__file__).resolve().parent.parent / "config.json")
        self.assertEqual(cfg["max_pages"], 200)
        sites = load_websites(cfg, Path(__file__).resolve().parent.parent / "websites.json")
        self.assertTrue(sites)


class AlertTests(unittest.TestCase):
    def test_sends_mail_for_down_sites_and_cross_domain_redirects(self):
        report = SiteReport(website="https://example.com")
        report.results.extend([
            UrlResult(
                website=report.website, url=report.website, is_start=True,
                error_type="connection_error",
            ),
            UrlResult(
                website=report.website, url=report.website + "/old",
                final_url="https://other.example/landing",
                redirect_category="cross_domain",
            ),
        ])
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text(
                "WEBSITE_MONITOR_EMAIL_TO=owner@example.com\n"
                "WEBSITE_MONITOR_SMTP_USERNAME=sender@example.com\n"
                "WEBSITE_MONITOR_SMTP_PASSWORD=app-password\n",
                encoding="utf-8",
            )
            with patch("alerts.smtplib.SMTP") as smtp_factory:
                smtp = smtp_factory.return_value.__enter__.return_value
                self.assertTrue(send_alert([report], Path(directory)))

        smtp_factory.assert_called_once_with("smtp.gmail.com", 587, timeout=30)
        smtp.starttls.assert_called_once()
        smtp.login.assert_called_once_with("sender@example.com", "app-password")
        message = smtp.send_message.call_args.args[0]
        self.assertIn("DOWN: https://example.com", message.get_content())
        self.assertIn("https://example.com/old -> https://other.example/landing",
                      message.get_content())

    def test_missing_smtp_password_is_reported(self):
        report = SiteReport(
            website="https://example.com",
            results=[UrlResult(
                website="https://example.com", url="https://example.com",
                is_start=True, error_type="connection_error",
            )],
        )
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, ".env").write_text(
                "WEBSITE_MONITOR_EMAIL_TO=owner@example.com\n"
                "WEBSITE_MONITOR_SMTP_USERNAME=sender@example.com\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(AlertError, "WEBSITE_MONITOR_SMTP_PASSWORD"):
                send_alert([report], Path(directory))

    def test_healthy_reports_do_not_send_email(self):
        report = SiteReport(
            website="https://example.com",
            results=[UrlResult(
                website="https://example.com", url="https://example.com",
                is_start=True, status=200,
            )],
        )
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(send_alert([report], Path(directory)))


class SslTests(unittest.IsolatedAsyncioTestCase):
    async def test_self_signed_certificate_is_ssl_error(self):
        if not shutil.which("openssl"):
            self.skipTest("openssl not available")
        tmp = tempfile.mkdtemp()
        cert, key = os.path.join(tmp, "c.pem"), os.path.join(tmp, "k.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-keyout", key,
                        "-out", cert, "-days", "1", "-subj", "/CN=localhost"],
                       check=True, capture_output=True)
        httpd = start_server((cert, key))
        cfg = test_cfg()
        client = make_client(cfg)
        try:
            res, _ = await Checker(client, HostThrottle(0, 2), cfg).check(
                "t", "https://127.0.0.1:%d/ok" % httpd.server_port)
            self.assertEqual(res.error_type, "ssl_error", res.error_detail)
        finally:
            await client.aclose()
            httpd.shutdown()
            shutil.rmtree(tmp, ignore_errors=True)


class CrawlTests(ServerTestCase):
    async def crawl(self, **overrides):
        cfg = test_cfg(**overrides)
        crawler = SiteCrawler(self.base, cfg, self.client, HostThrottle(0, 4))
        return await crawler.run()

    def by_path(self, report):
        return {r.url.replace(self.base, ""): r for r in report.results}

    async def test_full_crawl(self):
        rep = await self.crawl()
        res = self.by_path(rep)

        self.assertEqual(res["/"].status, 200)
        self.assertTrue(res["/"].is_start)
        # discovered pages
        self.assertEqual(res["/ok"].status, 200)
        # internal broken link, reported with the page it was found on
        self.assertEqual(res["/missing"].error_type, "http_404")
        self.assertEqual(res["/missing"].label, "Broken internal link")
        self.assertIn(self.base + "/", res["/missing"].found_on)
        self.assertEqual(res["/boom"].error_type, "http_500")
        self.assertEqual(res["/forbidden"].error_type, "http_4xx")
        # redirects found by crawling
        self.assertEqual(res["/old"].redirect_category, "attention")
        self.assertEqual(res["/chain1"].redirect_category, "excessive")
        self.assertEqual(res["/loop-a"].error_type, "redirect_loop")
        self.assertEqual(res["/cross"].redirect_category, "cross_domain")
        self.assertEqual(res["/dir"].redirect_category, "expected")
        # broken image + working image + stylesheet + pdf (HEAD rejected)
        self.assertEqual(res["/img/missing.png"].label, "Broken image/asset")
        self.assertEqual(res["/img/ok.png"].error_type, "")
        self.assertEqual(res["/style.css"].error_type, "")
        self.assertEqual(res["/files/report.pdf"].error_type, "")
        # duplicates collapsed: /p and /p?utm_source=x, /ok and /ok#frag
        self.assertEqual(sum(1 for p in res if p.startswith("/p") and p != "/private/secret"), 1)
        self.assertEqual(sum(1 for p in res if p.startswith("/ok")), 1)
        # robots.txt respected, mailto/tel/javascript ignored, externals not crawled
        self.assertNotIn("/private/secret", res)
        self.assertEqual(rep.skip_counts.get("blocked by robots.txt"), 1)
        self.assertFalse([r for r in rep.results if "mailto" in r.url or "other-site" in r.url])
        self.assertEqual(rep.skip_counts.get("external link (not checked)"), 1)
        # canonical captured
        self.assertEqual(res["/"].canonical, self.base + "/")
        # no URL checked twice
        urls = [r.url for r in rep.results]
        self.assertEqual(len(urls), len(set(urls)))

        s = rep.summary()
        self.assertEqual((s["e404"], s["e500"], s["e4xx"]), (2, 1, 1))   # /missing + missing.png
        self.assertEqual(s["redirect_loops"], 1)
        self.assertEqual(s["cross_domain"], 1)
        self.assertEqual(rep.status, "ERRORS")

    async def test_robots_can_be_ignored(self):
        rep = await self.crawl(respect_robots=False)
        self.assertIn("/private/secret", self.by_path(rep))

    async def test_max_pages_limit(self):
        rep = await self.crawl(max_pages=3, check_assets=False)
        pages = [r for r in rep.results if r.kind == "page"]
        self.assertEqual(len(pages), 3)
        self.assertTrue(rep.skip_counts.get("page limit reached"))
        self.assertTrue(any("Page limit" in n for n in rep.notes))

    async def test_ignore_pattern(self):
        rep = await self.crawl(ignore_patterns=["/boom"])
        self.assertNotIn("/boom", self.by_path(rep))

    async def test_external_links_checked_when_enabled(self):
        rep = await self.crawl(check_external_links=True)
        ext = [r for r in rep.results if r.external]
        self.assertEqual(len(ext), 1)
        self.assertEqual(ext[0].error_type, "dns_error")
        self.assertEqual(ext[0].label, "Broken external link")

    async def test_homepage_down(self):
        crawler = SiteCrawler("http://this-host-does-not-exist.invalid", test_cfg(),
                              self.client, HostThrottle(0, 2))
        rep = await crawler.run()
        self.assertEqual(rep.status, "DOWN")
        self.assertEqual(rep.results[0].label, "Site unreachable")

    async def test_reports_written(self):
        rep = await self.crawl()
        out = tempfile.mkdtemp()
        try:
            cfg = test_cfg(reports_dir=out)
            paths = reporter.write_reports([rep], cfg, datetime.now(), datetime.now(), Path(out))
            html = paths["html"].read_text(encoding="utf-8")
            for needle in ("Website health report", "Redirect loop", "Cross-domain redirect",
                           "Excessive redirect chain", "/missing", "Broken image/asset"):
                self.assertIn(needle, html)
            csv_text = paths["csv"].read_text(encoding="utf-8-sig")
            self.assertIn("http_404", csv_text)
            self.assertIn("redirect_loop", csv_text)
            self.assertTrue((Path(out) / "latest-report.html").exists())
            reporter.print_terminal([rep], cfg, paths, datetime.now(), datetime.now())
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Terminal summary, HTML report and CSV export."""
from __future__ import annotations

import csv
import os
import shutil
import sys
from datetime import datetime
from html import escape as esc
from pathlib import Path
from typing import Any, Dict, List

from models import ERROR_LABELS, REDIRECT_LABELS, SiteReport, UrlResult

USE_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _c(text: str, code: str) -> str:
    return "\033[%sm%s\033[0m" % (code, text) if USE_COLOR else text


def _secs(value: float) -> str:
    return "%.2fs" % value


def _row_class(r: UrlResult) -> str:
    if r.is_error:
        return "err"
    if r.unexpected_redirect or r.slow:
        return "warn"
    return "ok"


def _aggregate(reports: List[SiteReport]) -> Dict[str, int]:
    total: Dict[str, int] = {}
    for rep in reports:
        for k, v in rep.summary().items():
            total[k] = total.get(k, 0) + v
    return total


# ==========================================================================
# Terminal
# ==========================================================================
_STATUS_COLOR = {"OK": "32", "WARNINGS": "33", "ERRORS": "31", "DOWN": "1;31"}


def _print_problem(r: UrlResult) -> None:
    if r.is_error:
        print(_c("  ERROR", "31"))
        print("    Website:  %s" % r.website)
        print("    URL:      %s" % r.url)
        print("    Status:   %s" % (r.status if r.status is not None else "-"))
        print("    Type:     %s  [%s]" % (r.label, ERROR_LABELS.get(r.error_type, r.error_type)))
        if r.final_url and r.final_url != r.url:
            print("    Final:    %s" % r.final_url)
        if r.redirect_count:
            for line in r.chain_lines():
                print("              %s" % line)
        print("    Time:     %s" % _secs(r.response_time))
        print("    Details:  %s" % r.error_detail)
        if r.found_on:
            print("    Found on: %s" % ", ".join(r.found_on[:3]))
    elif r.unexpected_redirect:
        print(_c("  REDIRECT", "33"))
        print("    Website:   %s" % r.website)
        print("    URL:       %s" % r.url)
        print("    Status:    %s" % (r.chain[0].status if r.chain else "-"))
        print("    Final URL: %s" % r.final_url)
        print("    Redirects: %d  (%s)" % (r.redirect_count, REDIRECT_LABELS.get(r.redirect_category, "")))
        for line in r.chain_lines():
            print("               %s" % line)
    elif r.slow:
        print(_c("  SLOW", "35"))
        print("    Website:       %s" % r.website)
        print("    URL:           %s" % r.url)
        print("    Response time: %s" % _secs(r.response_time))


def print_terminal(reports: List[SiteReport], cfg: Dict[str, Any], paths: Dict[str, Path],
                   started: datetime, finished: datetime) -> None:
    limit = cfg["terminal_max_items"]
    total = _aggregate(reports)
    bar = "=" * 72
    print("\n" + bar)
    print(_c("WEBSITE HEALTH REPORT", "1") + "  " + started.strftime("%Y-%m-%d %H:%M"))
    print(bar)

    for rep in reports:
        s = rep.summary()
        status = _c("[%s]" % rep.status, _STATUS_COLOR.get(rep.status, "0"))
        print("\n\u25cf %s  %s" % (_c(rep.host or rep.website, "1"), status))
        print("    discovered %d \u00b7 checked %d \u00b7 healthy %d \u00b7 %.0fs"
              % (s["discovered"], s["checked"], s["healthy"], rep.duration))
        print("    404: %d   other 4xx: %d   500: %d   other 5xx: %d   other errors: %d   broken links: %d"
              % (s["e404"], s["e4xx"], s["e500"], s["e5xx"], s["other_errors"], s["broken_links"]))
        print("    redirects: %d   chains: %d   loops: %d   cross-domain: %d   need attention: %d"
              % (s["redirects"], s["redirect_chains"], s["redirect_loops"], s["cross_domain"],
                 s["redirect_attention"]))
        print("    slow: %d   connection failures: %d   SSL/DNS failures: %d"
              % (s["slow"], s["connection_failures"], s["ssl_dns_failures"]))
        for note in rep.notes:
            print("    note: " + note)

    print("\n" + bar)
    print("ALL WEBSITES: %d sites \u00b7 %d URLs checked \u00b7 %s \u00b7 %s \u00b7 %s"
          % (len(reports), total.get("checked", 0),
             _c("%d errors" % total.get("errors", 0), "31" if total.get("errors") else "32"),
             _c("%d redirects to review" % total.get("redirect_attention", 0), "33"),
             _c("%d slow" % total.get("slow", 0), "35")))
    print(bar)

    any_problem = False
    for rep in reports:
        sections = [
            [r for r in rep.results if r.is_error],
            [r for r in rep.results if r.unexpected_redirect and not r.is_error],
            rep.slow_urls(),
        ]
        if not any(sections):
            continue
        any_problem = True
        print("\n" + _c("\u2500\u2500 %s " % (rep.host or rep.website), "1") + "\u2500" * 40)
        for items in sections:
            for r in items[:limit]:
                _print_problem(r)
            if len(items) > limit:
                print("  ... and %d more (see the report file)" % (len(items) - limit))
    if not any_problem:
        print(_c("\nNo problems found.", "32"))

    print("\nHTML report: %s" % paths["html"])
    print("CSV report:  %s" % paths["csv"])
    print("Finished in %.0f seconds." % (finished - started).total_seconds())


# ==========================================================================
# CSV
# ==========================================================================
CSV_COLUMNS = [
    "website", "url", "type", "external", "problem", "http_status", "error_type", "error_details",
    "final_url", "redirect_count", "redirect_category", "redirect_location", "redirect_chain",
    "final_same_domain", "response_time_s", "slow", "canonical", "found_on", "notes",
]


def write_csv(reports: List[SiteReport], path: Path) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(CSV_COLUMNS)
        for rep in reports:
            for r in rep.results:
                w.writerow([
                    rep.host or rep.website, r.url, r.kind, "yes" if r.external else "no",
                    "yes" if _row_class(r) != "ok" else "no",
                    r.status if r.status is not None else "", r.error_type, r.error_detail,
                    r.final_url, r.redirect_count, r.redirect_category, r.first_redirect_location,
                    " | ".join(r.chain_lines()) if r.redirect_count else "",
                    "yes" if r.final_same_domain else "no", "%.3f" % r.response_time,
                    "yes" if r.slow else "no", r.canonical, " | ".join(r.found_on),
                    " | ".join(r.notes)])


# ==========================================================================
# HTML
# ==========================================================================
CSS = """
:root{--bg:#f6f7f9;--card:#fff;--text:#1d2530;--muted:#657080;--line:#e2e6ec;
--ok:#1a8f4c;--warn:#b7791f;--err:#c53030;--info:#2b6cb0;--okbg:#e6f6ed;--warnbg:#fdf3dc;--errbg:#fde8e8;--infobg:#e6f0fb}
@media (prefers-color-scheme:dark){:root{--bg:#14171c;--card:#1c2027;--text:#e6e9ee;--muted:#98a2b3;--line:#2c323c;
--ok:#4cc38a;--warn:#e6b450;--err:#ff7b7b;--info:#6cb0ff;--okbg:#17301f;--warnbg:#33290f;--errbg:#3a1a1a;--infobg:#172a40}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.45 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif}
.wrap{max-width:1300px;margin:0 auto;padding:24px 20px 60px}
h1{margin:0 0 4px;font-size:24px}h2{margin:34px 0 10px;font-size:18px}
.muted{color:var(--muted)}
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin:18px 0}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card b{display:block;font-size:26px;line-height:1.1}.card span{color:var(--muted);font-size:12px}
.card.err b{color:var(--err)}.card.warn b{color:var(--warn)}.card.ok b{color:var(--ok)}
.scroll{overflow-x:auto;background:var(--card);border:1px solid var(--line);border-radius:10px}
table{border-collapse:collapse;width:100%}
th,td{padding:7px 10px;text-align:left;vertical-align:top;border-bottom:1px solid var(--line);font-size:13px}
th{background:var(--bg);font-weight:600;white-space:nowrap;position:sticky;top:0}
tr:last-child td{border-bottom:none}
td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.url{word-break:break-all;min-width:220px}
a{color:var(--info);text-decoration:none}a:hover{text-decoration:underline}
.badge{display:inline-block;padding:1px 8px;border-radius:999px;font-size:11px;font-weight:600;white-space:nowrap}
.badge.ok{background:var(--okbg);color:var(--ok)}.badge.warn{background:var(--warnbg);color:var(--warn)}
.badge.err{background:var(--errbg);color:var(--err)}.badge.info{background:var(--infobg);color:var(--info)}
.dot{display:inline-block;width:10px;height:10px;border-radius:50%;margin-right:6px}
.dot.ok{background:var(--ok)}.dot.warn{background:var(--warn)}.dot.err{background:var(--err)}
pre.chain{margin:4px 0 0;white-space:pre-wrap;word-break:break-all;font:12px/1.4 ui-monospace,Menlo,monospace;color:var(--muted)}
details.site{background:var(--card);border:1px solid var(--line);border-radius:10px;margin:10px 0}
details.site>summary{cursor:pointer;padding:12px 16px;font-weight:600}
details.site .scroll{border:none;border-top:1px solid var(--line);border-radius:0}
.tools{display:flex;gap:10px;flex-wrap:wrap;margin:8px 0}
.tools input,.tools select,.tools button{padding:6px 10px;border:1px solid var(--line);border-radius:6px;background:var(--card);color:var(--text);font:inherit}
.tools input{min-width:260px}
.empty{padding:14px 16px;color:var(--muted)}
.note{background:var(--warnbg);color:var(--warn);padding:8px 12px;border-radius:8px;margin:8px 0;font-size:13px}
"""

JS = """
function applyFilters(){
  var q=document.getElementById('q').value.toLowerCase();
  var onlyProblems=document.getElementById('pf').value==='problems';
  document.querySelectorAll('table.detail tbody tr').forEach(function(tr){
    var ok=(!q||tr.textContent.toLowerCase().indexOf(q)!==-1)&&(!onlyProblems||tr.dataset.problem==='1');
    tr.style.display=ok?'':'none';
  });
}
document.getElementById('q').addEventListener('input',applyFilters);
document.getElementById('pf').addEventListener('change',applyFilters);
document.getElementById('expand').addEventListener('click',function(){
  var ds=document.querySelectorAll('details.site');
  var open=Array.prototype.some.call(ds,function(d){return !d.open;});
  ds.forEach(function(d){d.open=open;});
  this.textContent=open?'Collapse all':'Expand all';
});
"""


def _link(url: str) -> str:
    return '<a href="%s" target="_blank" rel="noopener noreferrer">%s</a>' % (esc(url, quote=True), esc(url))


def _badge(text: str, cls: str) -> str:
    return '<span class="badge %s">%s</span>' % (cls, esc(text))


def _status_badge(r: UrlResult) -> str:
    if r.is_error:
        return _badge(str(r.status) if r.status is not None else "ERR", "err")
    cls = "warn" if (r.unexpected_redirect or r.slow) else ("info" if r.has_redirect else "ok")
    return _badge(str(r.status), cls)


def _chain_html(r: UrlResult) -> str:
    if not r.redirect_count:
        return ""
    return '<pre class="chain">%s</pre>' % esc("\n".join(r.chain_lines()))


def _found_on_html(r: UrlResult) -> str:
    if not r.found_on:
        return '<span class="muted">-</span>'
    return "<br>".join(_link(u) for u in r.found_on[:5])


def _table(headers: List[str], rows: List[str], numeric: Any = ()) -> str:
    if not rows:
        return '<div class="scroll"><div class="empty">Nothing to report \u2714</div></div>'
    head = "".join('<th class="num">%s</th>' % esc(h) if i in numeric else "<th>%s</th>" % esc(h)
                   for i, h in enumerate(headers))
    return '<div class="scroll"><table><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div>' % (
        head, "".join(rows))


_SUMMARY_COLS = [
    ("Found", "discovered"), ("Checked", "checked"), ("Healthy", "healthy"), ("404", "e404"),
    ("Other 4xx", "e4xx"), ("500", "e500"), ("Other 5xx", "e5xx"), ("Other errors", "other_errors"),
    ("Broken links", "broken_links"), ("Redirects", "redirects"), ("Chains", "redirect_chains"),
    ("Loops", "redirect_loops"), ("Cross-domain", "cross_domain"), ("Slow", "slow"),
    ("Conn. fail", "connection_failures"), ("SSL/DNS", "ssl_dns_failures"),
]
_BADGE = {"OK": "ok", "WARNINGS": "warn", "ERRORS": "err", "DOWN": "err"}


def build_html(reports: List[SiteReport], started: datetime, finished: datetime) -> str:
    total = _aggregate(reports)
    p: List[str] = []
    p.append('<!doctype html><html lang="en"><head><meta charset="utf-8">'
             '<meta name="viewport" content="width=device-width,initial-scale=1">'
             "<title>Website health report %s</title><style>%s</style></head><body><div class=\"wrap\">"
             % (started.strftime("%Y-%m-%d %H:%M"), CSS))
    p.append("<h1>Website health report</h1>")
    p.append('<div class="muted">Run %s \u00b7 took %.0f seconds \u00b7 %d websites</div>'
             % (started.strftime("%Y-%m-%d %H:%M"), (finished - started).total_seconds(), len(reports)))

    # ---- overall summary ---------------------------------------------------
    down = sum(1 for r in reports if r.status == "DOWN")
    p.append('<h2>Overall summary</h2><div class="cards">')
    for label, value, cls in [
        ("Websites", len(reports), ""), ("Sites down", down, "err" if down else "ok"),
        ("URLs checked", total.get("checked", 0), ""), ("Healthy URLs", total.get("healthy", 0), "ok"),
        ("Errors", total.get("errors", 0), "err" if total.get("errors") else "ok"),
        ("Broken links", total.get("broken_links", 0), "err" if total.get("broken_links") else "ok"),
        ("Redirects to review", total.get("redirect_attention", 0), "warn" if total.get("redirect_attention") else "ok"),
        ("Slow URLs", total.get("slow", 0), "warn" if total.get("slow") else "ok"),
    ]:
        p.append('<div class="card %s"><b>%d</b><span>%s</span></div>' % (cls, value, esc(label)))
    p.append("</div>")

    # ---- website by website -------------------------------------------------
    rows = []
    for rep in reports:
        s = rep.summary()
        cells = "".join('<td class="num">%d</td>' % s[k] for _, k in _SUMMARY_COLS)
        rows.append('<tr><td><b>%s</b></td><td>%s</td>%s</tr>'
                    % (esc(rep.host or rep.website), _badge(rep.status, _BADGE[rep.status]), cells))
    p.append("<h2>Website-by-website summary</h2>")
    p.append(_table(["Website", "Status"] + [h for h, _ in _SUMMARY_COLS], rows,
                    numeric=set(range(2, 2 + len(_SUMMARY_COLS)))))
    for rep in reports:
        for note in rep.notes:
            p.append('<div class="note"><b>%s:</b> %s</div>' % (esc(rep.host or rep.website), esc(note)))

    # ---- errors --------------------------------------------------------------
    err_rows = []
    for rep in reports:
        for r in sorted(rep.errors(), key=lambda x: (not x.is_start, x.error_type, x.url)):
            err_rows.append(
                "<tr><td>%s</td><td>%s<br><span class=\"muted\">%s</span></td><td class=\"url\">%s</td>"
                "<td>%s</td><td>%s</td><td class=\"url\">%s%s</td><td class=\"num\">%s</td><td>%s</td></tr>"
                % (esc(rep.host), _badge(r.label, "err"), esc(ERROR_LABELS.get(r.error_type, r.error_type)),
                   _link(r.url), _status_badge(r), esc(r.error_detail),
                   _link(r.final_url) if r.final_url and r.final_url != r.url else "", _chain_html(r),
                   _secs(r.response_time), _found_on_html(r)))
    p.append("<h2>Errors (%d)</h2>" % len(err_rows))
    p.append(_table(["Website", "Type", "URL", "Status", "Error details", "Final URL / redirect chain",
                     "Time", "Found on"], err_rows, numeric={6}))

    # ---- redirects -----------------------------------------------------------
    order = {"loop": 0, "cross_domain": 1, "excessive": 2, "attention": 3, "expected": 4}
    redir_rows, expected_rows = [], []
    for rep in reports:
        for r in sorted(rep.redirects(), key=lambda x: (order.get(x.redirect_category, 5), x.url)):
            cls = "info" if r.redirect_category == "expected" else "warn"
            row = ("<tr><td>%s</td><td class=\"url\">%s</td><td>%s</td><td class=\"url\">%s</td>"
                   "<td class=\"num\">%d</td><td>%s</td><td>%s</td><td>%s</td></tr>"
                   % (esc(rep.host), _link(r.url), _badge(str(r.chain[0].status) if r.chain and r.chain[0].status else "-", cls),
                      _link(r.final_url) + _chain_html(r), r.redirect_count,
                      _badge(REDIRECT_LABELS.get(r.redirect_category, r.redirect_category), cls),
                      "yes" if r.final_same_domain else "<b>no</b>",
                      _found_on_html(r)))
            (expected_rows if r.redirect_category == "expected" else redir_rows).append(row)
    heads = ["Website", "URL", "Status", "Final URL / chain", "Redirects", "Category", "Same domain", "Found on"]
    p.append("<h2>Redirects needing attention (%d)</h2>" % len(redir_rows))
    p.append(_table(heads, redir_rows, numeric={4}))
    p.append('<details class="site"><summary>Expected redirects (%d) &mdash; HTTP\u2192HTTPS, www, trailing slash, your rules</summary>%s</details>'
             % (len(expected_rows), _table(heads, expected_rows, numeric={4})))

    # ---- slow ------------------------------------------------------------------
    slow_rows = []
    for rep in reports:
        for r in rep.slow_urls():
            slow_rows.append("<tr><td>%s</td><td class=\"url\">%s</td><td>%s</td><td class=\"num\"><b>%s</b></td></tr>"
                             % (esc(rep.host), _link(r.url), _status_badge(r), _secs(r.response_time)))
    p.append("<h2>Slow URLs (%d)</h2>" % len(slow_rows))
    p.append(_table(["Website", "URL", "Status", "Response time"], slow_rows, numeric={3}))

    # ---- broken links --------------------------------------------------------
    broken_rows = []
    for rep in reports:
        for r in rep.broken_links():
            broken_rows.append("<tr><td>%s</td><td>%s</td><td class=\"url\">%s</td><td>%s</td><td>%s</td></tr>"
                               % (esc(rep.host), esc(r.label), _link(r.url), _status_badge(r), _found_on_html(r)))
    p.append("<h2>Broken links (%d)</h2>" % len(broken_rows))
    p.append(_table(["Website", "Kind", "Broken URL", "Status", "Linked from"], broken_rows))

    # ---- detailed results -----------------------------------------------------
    p.append("<h2>Detailed URL results</h2>")
    p.append('<div class="tools"><input id="q" type="search" placeholder="Filter URLs, statuses, errors\u2026">'
             '<select id="pf"><option value="all">All URLs</option><option value="problems">Problems only</option></select>'
             '<button id="expand" type="button">Expand all</button></div>')
    rank = {"err": 0, "warn": 1, "ok": 2}
    for rep in reports:
        s = rep.summary()
        p.append('<details class="site"><summary>%s %s <span class="muted">&middot; %d checked</span></summary>'
                 % (esc(rep.host or rep.website), _badge(rep.status, _BADGE[rep.status]), s["checked"]))
        if rep.skip_counts:
            p.append('<div class="empty">Not checked: %s</div>' % esc(
                "; ".join("%s (%d)" % (k, v) for k, v in sorted(rep.skip_counts.items()))))
        rows = []
        for r in sorted(rep.results, key=lambda x: (rank[_row_class(x)], not x.is_start, x.url)):
            cls = _row_class(r)
            problem = "1" if (cls != "ok" or r.has_redirect) else "0"
            tail = esc(r.error_detail) if r.is_error else "; ".join(esc(n) for n in r.notes)
            final = ""
            if r.final_url and r.final_url != r.url:
                final = _link(r.final_url)
            rows.append(
                '<tr data-problem="%s"><td><span class="dot %s"></span>%s</td><td class="url">%s</td>'
                "<td>%s</td><td>%s</td><td class=\"url\">%s%s</td><td>%s</td><td class=\"num\">%s</td><td>%s</td></tr>"
                % (problem, cls, _status_badge(r), _link(r.url),
                   esc(r.kind + (" (external)" if r.external else "")),
                   esc(ERROR_LABELS.get(r.error_type, "") if r.is_error else ""),
                   final, _chain_html(r),
                   esc(REDIRECT_LABELS.get(r.redirect_category, "")) if r.has_redirect else "",
                   _secs(r.response_time) + (" \U0001F422" if r.slow else ""), tail))
        head = "".join("<th>%s</th>" % h for h in
                       ["Status", "URL", "Type", "Error", "Final URL / chain", "Redirect", "Time", "Details"])
        p.append('<div class="scroll"><table class="detail"><thead><tr>%s</tr></thead><tbody>%s</tbody></table></div></details>'
                 % (head, "".join(rows)))
    p.append("</div><script>%s</script></body></html>" % JS)
    return "".join(p)


# ==========================================================================
# Entry point used by main.py
# ==========================================================================
def write_reports(reports: List[SiteReport], cfg: Dict[str, Any], started: datetime,
                  finished: datetime, base_dir: Path) -> Dict[str, Path]:
    out_dir = Path(cfg["reports_dir"]).expanduser()
    if not out_dir.is_absolute():
        out_dir = base_dir / out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = started.strftime("%Y-%m-%d-%H-%M")
    html_path = out_dir / ("%s-report.html" % stamp)
    csv_path = out_dir / ("%s-report.csv" % stamp)
    html_path.write_text(build_html(reports, started, finished), encoding="utf-8")
    write_csv(reports, csv_path)
    shutil.copyfile(html_path, out_dir / "latest-report.html")
    return {"html": html_path, "csv": csv_path}

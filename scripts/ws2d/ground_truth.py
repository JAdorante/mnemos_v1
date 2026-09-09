#!/usr/bin/env python3
"""WS2d Phase 0 gate — per-browser domain precision and recall.

Playwright is both the navigation driver AND the ground-truth oracle: it knows
what it navigated to and `page.url` is the committed URL. The source under test
(UIA on Windows, AT-SPI on Linux) reads the same foreground window completely
independently, and the two are diffed.

    python3 scripts/ws2d/ground_truth.py --browser chromium
    python3 scripts/ws2d/ground_truth.py --browser firefox --repeat 3 --out gt.json

The gate, from the brief, is stated PER BROWSER and never aggregated:
  precision >= 99%   of the reads that returned something, this fraction was
                     the right registrable domain. A wrong domain is worse
                     than no domain, so this is the number that decides.
  recall    >= 80%   of navigations produced any read at all.
  p95       <= 150ms

Precision and recall are reported per navigation CASE as well, because the
failure modes are not uniform: same-title SPA routing is a KNOWN miss of the
(hwnd, title) cache key and should show up as a recall hole, not a surprise.

Not automatable here, and deliberately not faked: typing into the omnibox
without submitting. That is browser chrome, not page content, and Playwright
cannot reach it. Run with --manual to get a pause and a prompt for it.
"""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.perception import psl                                   # noqa: E402
from scripts.ws2d.url_sources import get_source                  # noqa: E402

# Stable, low-churn destinations across several registrable domains, including
# a ccTLD second-level registry (co.uk) that the old last-two-labels split got
# wrong. Override with --urls-file (one URL per line).
DEFAULT_URLS = [
    "https://www.bbc.co.uk/news",
    "https://github.com/python/cpython",
    "https://en.wikipedia.org/wiki/Public_Suffix_List",
    "https://developer.mozilla.org/en-US/docs/Web/API",
    "https://news.ycombinator.com/",
    "https://www.gov.uk/",
]

SPA_HTML = """<!doctype html><title>Steady Title</title><body><h1>spa</h1>
<script>
  let n = 0;
  window.step = () => history.pushState({}, '', '/route-' + (++n));
</script></body>"""

SPA_ORIGIN = "https://spa.example.com"

PDF_URL = "https://www.w3.org/WAI/ER/tests/xhtml/testfiles/resources/pdf/dummy.pdf"


class Result:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, case: str, truth: str | None, got: str | None,
            ms: float, truth_after: str | None = None) -> None:
        """`truth_after` is the driver's URL sampled AFTER the read.

        A site that redirects (bbc.co.uk -> bbc.com) is legitimately at two
        URLs across the read window, and the driver and the accessibility
        layer do not observe the switch at the same instant. Counting that as
        a precision failure would blame the reader for the harness's own race,
        so either truth counts as correct. Both are recorded, so a genuinely
        wrong read still shows up with neither matching.
        """
        doms = [d for d in (psl.registrable_domain(truth or "") if truth else None,
                            psl.registrable_domain(truth_after or "")
                            if truth_after else None) if d]
        g_dom = psl.registrable_domain(got or "") if got else None
        self.rows.append({
            "case": case, "truth": truth, "truth_after": truth_after,
            "truth_domain": doms[0] if doms else None,
            "truth_domains": doms,
            "read": got, "read_domain": g_dom,
            "read_returned": g_dom is not None,
            "correct": bool(g_dom and g_dom in doms),
            # Path-level agreement is NOT gated — the shipped default stores
            # the domain only. It is recorded because it is the number that
            # shows the (hwnd, title) cache key missing same-title SPA
            # routing: domain right, path stale.
            "path_correct": bool(got and (got in (truth, truth_after))),
            "ms": round(ms, 1),
        })

    def summary(self, browser: str, source: str) -> dict:
        rows = self.rows
        returned = [r for r in rows if r["read_returned"]]
        correct = [r for r in returned if r["correct"]]
        lat = [r["ms"] for r in rows] or [0.0]
        by_case: dict[str, dict] = {}
        for r in rows:
            c = by_case.setdefault(r["case"], {"n": 0, "returned": 0, "correct": 0})
            c["n"] += 1
            c["returned"] += int(r["read_returned"])
            c["correct"] += int(r["correct"])
            c["path_correct"] = c.get("path_correct", 0) + int(r["path_correct"])
        precision = (len(correct) / len(returned)) if returned else None
        recall = (len(returned) / len(rows)) if rows else None
        p95 = (statistics.quantiles(lat, n=20)[-1] if len(lat) > 1 else lat[0])
        out = {
            "browser": browser, "source": source, "navigations": len(rows),
            "reads_returned": len(returned), "reads_correct": len(correct),
            "precision": None if precision is None else round(precision, 4),
            "recall": None if recall is None else round(recall, 4),
            "latency_p50_ms": round(statistics.median(lat), 1),
            "latency_p95_ms": round(p95, 1),
            "by_case": by_case,
            "wrong": [r for r in returned if not r["correct"]][:20],
        }
        out["gate"] = {
            "precision_ge_0.99": bool(precision is not None and precision >= 0.99),
            "recall_ge_0.80": bool(recall is not None and recall >= 0.80),
        }
        # The 150 ms budget is a claim about the SHIPPED reader — one UIA
        # property fetch. The AT-SPI source is a breadth-first tree walk over
        # D-Bus and is inherently slower; scoring it against that budget would
        # be measuring the harness, not the product. Reported, not gated.
        if source == "uia":
            out["gate"]["p95_le_150ms"] = bool(out["latency_p95_ms"] <= 150.0)
        else:
            out["latency_note"] = ("atspi source is a D-Bus tree walk — "
                                   "latency reported, not gated")
        out["pass"] = all(out["gate"].values())
        return out


def _settle_url(page, timeout_s: float = 6.0) -> None:
    """Wait until the driver's own URL stops moving.

    Client-side redirects (bbc.co.uk -> bbc.com) leave `page.url` reporting the
    pre-redirect address while the browser is already elsewhere. Sampling then
    makes the ORACLE wrong and scores a correct read as a precision failure.
    Poll until it holds still, then measure.
    """
    try:
        page.wait_for_load_state("load", timeout=int(timeout_s * 1000))
    except Exception:
        pass
    deadline, last = time.time() + timeout_s, None
    while time.time() < deadline:
        try:
            cur = page.url
        except Exception:
            return
        if cur == last:
            return
        last = cur
        time.sleep(0.35)


def _sample(source, page, settle_s: float):
    """Foreground the window, let the accessibility layer catch up, read.

    Returns (read, ms, url_before, url_after) — the two driver observations
    bracketing the read, so a redirect mid-window is not scored as a miss.
    """
    try:
        page.bring_to_front()
    except Exception:
        pass
    _settle_url(page)
    time.sleep(settle_s)

    def _url():
        """The document's OWN view of its location.

        MEASURED: `page.url` is not a trustworthy oracle across a redirect.
        Navigating to bbc.co.uk/news lands on bbc.com/news — every document in
        the accessibility tree said bbc.com while `page.url` still said
        bbc.co.uk, which scored a CORRECT read as a precision failure. The
        oracle has to come from inside the document.
        """
        try:
            href = page.evaluate("location.href")
            if href:
                return str(href)
        except Exception:
            pass
        try:
            return page.url
        except Exception:
            return None

    before = _url()
    t0 = time.time()
    got = source.read()
    ms = (time.time() - t0) * 1000.0
    return got, ms, before, _url()


def run(browser_name: str, source, urls: list[str], settle_s: float,
        repeat: int, manual: bool) -> Result:
    from playwright.sync_api import sync_playwright

    res = Result()
    with sync_playwright() as pw:
        launcher = {"chromium": pw.chromium, "firefox": pw.firefox,
                    "webkit": pw.webkit}.get(browser_name)
        kwargs: dict = {"headless": False}
        if browser_name in ("chrome", "msedge"):
            launcher, kwargs["channel"] = pw.chromium, browser_name
        if launcher is None:
            raise SystemExit(f"unknown browser {browser_name!r}")
        if browser_name in ("chromium", "chrome", "msedge"):
            kwargs["args"] = ["--no-sandbox"]
        browser = launcher.launch(**kwargs)
        ctx = browser.new_context()
        page = ctx.new_page()
        try:
            for _ in range(repeat):
                # 1) plain cross-domain navigations
                for url in urls:
                    try:
                        page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    except Exception as exc:
                        print(f"  [skip] {url}: {str(exc)[:80]}")
                        continue
                    got, ms, u0, u1 = _sample(source, page, settle_s)
                    res.add("navigate", u0, got, ms, u1)
                    print(f"  navigate   truth={str(u1)[:55]:<55} read={str(got)[:55]}")

                # 2) page mid-load — committed but not finished
                try:
                    page.goto(urls[0], wait_until="commit", timeout=30000)
                    got, ms, u0, u1 = _sample(source, page, 0.0)
                    res.add("mid_load", u0, got, ms, u1)
                    print(f"  mid_load   read={str(got)[:55]}")
                except Exception as exc:
                    print(f"  [skip] mid_load: {str(exc)[:80]}")

                # 3) SPA route change with an UNCHANGED title — the known miss
                #    of the (hwnd, window_title) cache key. Served through
                #    request interception rather than a data: URL, because
                #    pushState needs a real origin (a data: document is
                #    origin-null and History refuses).  No network involved.
                page.route(SPA_ORIGIN + "/**", lambda route: route.fulfill(
                    status=200, content_type="text/html", body=SPA_HTML))
                page.goto(SPA_ORIGIN + "/", wait_until="domcontentloaded")
                _sample(source, page, settle_s)          # warm the cache
                for _ in range(2):
                    page.evaluate("window.step()")
                    got, ms, u0, u1 = _sample(source, page, settle_s)
                    res.add("spa_same_title", u0, got, ms, u1)
                    print(f"  spa        truth={str(u1)[:55]:<55} read={str(got)[:55]}")

                # 4) a second tab, then switching back
                tab = ctx.new_page()
                try:
                    tab.goto(urls[1 % len(urls)], wait_until="domcontentloaded",
                             timeout=30000)
                    got, ms, u0, u1 = _sample(source, tab, settle_s)
                    res.add("new_tab", u0, got, ms, u1)
                    print(f"  new_tab    truth={tab.url[:55]:<55} read={str(got)[:55]}")
                    got, ms, u0, u1 = _sample(source, page, settle_s)
                    res.add("tab_switch_back", u0, got, ms, u1)
                    print(f"  tab_back   truth={page.url[:55]:<55} read={str(got)[:55]}")
                except Exception as exc:
                    print(f"  [skip] new_tab: {str(exc)[:80]}")
                finally:
                    tab.close()

                # 5) a separate window
                ctx2 = browser.new_context()
                win = ctx2.new_page()
                try:
                    win.goto(urls[2 % len(urls)], wait_until="domcontentloaded",
                             timeout=30000)
                    got, ms, u0, u1 = _sample(source, win, settle_s)
                    res.add("new_window", u0, got, ms, u1)
                    print(f"  new_window truth={win.url[:55]:<55} read={str(got)[:55]}")
                except Exception as exc:
                    print(f"  [skip] new_window: {str(exc)[:80]}")
                finally:
                    ctx2.close()

                # 6) PDF viewer tab
                try:
                    page.goto(PDF_URL, wait_until="commit", timeout=30000)
                    time.sleep(2.0)
                    got, ms, u0, u1 = _sample(source, page, settle_s)
                    res.add("pdf_tab", u0, got, ms, u1)
                    print(f"  pdf        truth={page.url[:55]:<55} read={str(got)[:55]}")
                except Exception as exc:
                    print(f"  [skip] pdf: {str(exc)[:80]}")

                # 7) browser under load — many tabs open at once
                loaded = []
                try:
                    for u in urls[:4]:
                        t = ctx.new_page()
                        loaded.append(t)
                        t.goto(u, wait_until="commit", timeout=30000)
                    got, ms, u0, u1 = _sample(source, loaded[-1], settle_s)
                    res.add("under_load", u0, got, ms, u1)
                    print(f"  under_load truth={loaded[-1].url[:55]:<55} read={str(got)[:55]}")
                except Exception as exc:
                    print(f"  [skip] under_load: {str(exc)[:80]}")
                finally:
                    for t in loaded:
                        try:
                            t.close()
                        except Exception:
                            pass

            if manual:
                page.goto(urls[0], wait_until="domcontentloaded")
                print("\n  MANUAL CASE — click the address bar and TYPE a search "
                      "WITHOUT pressing Enter, then press Enter here.")
                input("  ready> ")
                got, ms, u0, u1 = _sample(source, page, settle_s)
                # Ground truth is the loaded page, NOT what is typed. A read
                # that returns the typed text is the omnibox trap firing.
                res.add("omnibox_typing", u0, got, ms, u1)
                print(f"  omnibox    truth={page.url[:55]:<55} read={str(got)[:55]}")
        finally:
            ctx.close()
            browser.close()
    return res


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", default="chromium",
                    help="chromium|firefox|chrome|msedge")
    ap.add_argument("--source", default="auto", help="auto|uia|atspi")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--settle", type=float, default=1.2,
                    help="seconds to let the a11y layer catch up before a read")
    ap.add_argument("--urls-file", default="")
    ap.add_argument("--manual", action="store_true",
                    help="pause for the omnibox-typing case")
    ap.add_argument("--out", default="")
    ap.add_argument("--strict", action="store_true",
                    help="exit 2 when the per-browser gate fails")
    args = ap.parse_args()

    urls = DEFAULT_URLS
    if args.urls_file:
        urls = [l.strip() for l in Path(args.urls_file).read_text().splitlines()
                if l.strip() and not l.startswith("#")]

    source = get_source(args.source)
    ok, why = source.available()
    print(f"source: {source.name} ({why})")
    if not ok:
        print("URL source unavailable — nothing to measure.")
        return 1

    restore = None
    if source.name == "atspi":
        from scripts.ws2d.url_sources import AtspiSource
        was = (AtspiSource.accessibility_enabled(),
               AtspiSource.screen_reader_enabled())
        if not was[1]:
            print("  enabling org.a11y.Status.ScreenReaderEnabled "
                  "(Chromium leaves web contents unpopulated without it)")
            AtspiSource.set_accessibility(True)
            AtspiSource.set_screen_reader(True)
            source.close()
            source = get_source(args.source)
            source.available()

        def restore() -> None:                                    # noqa: F811
            AtspiSource.set_accessibility(bool(was[0]))
            AtspiSource.set_screen_reader(bool(was[1]))
            print(f"  restored desktop a11y flags to {was}")

    print(f"browser: {args.browser}   navigations: ~{13 * args.repeat}\n")
    try:
        res = run(args.browser, source, urls, args.settle, args.repeat,
                  args.manual)
    finally:
        source.close()
        if restore:
            restore()

    summary = res.summary(args.browser, source.name)
    print("\n" + json.dumps({k: v for k, v in summary.items()
                             if k not in ("wrong",)}, indent=2))
    if summary["wrong"]:
        print("\nWRONG READS (precision failures — the ones that matter):")
        for r in summary["wrong"]:
            print(f"  [{r['case']}] truth={r['truth_domain']} read={r['read_domain']}"
                  f"  ({str(r['read'])[:70]})")
    if args.out:
        Path(args.out).write_text(json.dumps(
            {"summary": summary, "rows": res.rows}, indent=2), encoding="utf-8")
        print(f"\nwrote {args.out}")

    print("\nGATE (per browser, never aggregated): "
          + ("PASS" if summary["pass"] else "FAIL"))
    for k, v in summary["gate"].items():
        print(f"  {'ok ' if v else 'FAIL'} {k}")
    if args.strict and not summary["pass"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

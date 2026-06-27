#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
diag_boell.py  —  find a transport that gets past Böll's 503.

Walks the full ladder against the Böll listing (and one detail page) and reports,
per transport, whether it returned real HTML or a block page (and what kind of
block), then whether the jobs selector finds the postings. Designed to run as a
GitHub Actions job (see .github/workflows/diag-boell.yml) so it uses the RUNNER's
IP and reads CF_WORKER_URL / CF_WORKER_TOKEN from repo secrets — you don't need to
know the token. Also runnable locally: `py diag_boell.py`.

Ladder: plain -> curl_cffi (TLS impersonation) -> cloudscraper -> Cloudflare
Worker proxy -> jina reader (off-runner + JS) -> Playwright+stealth (real browser).
"""
import os, re, time
import requests
from bs4 import BeautifulSoup

try:
    from curl_cffi import requests as cffi
    HAVE_CFFI = True
except Exception:
    HAVE_CFFI = False
try:
    import cloudscraper
    HAVE_CS = True
except Exception:
    HAVE_CS = False
try:
    from playwright.sync_api import sync_playwright
    HAVE_PW = True
except Exception:
    HAVE_PW = False
try:
    from playwright_stealth import stealth_sync
except Exception:
    stealth_sync = None

LISTING = os.environ.get("BOELL_URL", "https://www.boell.de/de/jobs-der-heinrich-boell-stiftung")
DETAIL  = os.environ.get("BOELL_DETAIL_URL",
    "https://www.boell.de/de/2026/06/26/studentische-teilzeitkraft-archiv-gruenes-gedaechtnis-wmdka")

CF_WORKER_URL   = os.environ.get("CF_WORKER_URL", "")
CF_WORKER_TOKEN = os.environ.get("CF_WORKER_TOKEN", "")
JINA_API_KEY    = os.environ.get("JINA_API_KEY", "")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA,
           "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "Accept-Language": "de-DE,de;q=0.9,en;q=0.8"}

# jobs selector (confirmed against the captured page) + fallbacks for comparison
SELECTORS = [".views-row a[rel='bookmark']",
             "article.node--type-article a[rel='bookmark']",
             "a[href*='/de/20']"]
NO_VACANCY = ["keine offenen stellen", "keine stellenangebote", "derzeit keine",
              "zurzeit keine", "aktuell keine", "no vacancies", "no current vacancies"]


def block_kind(html):
    h = html.lower()
    if "cf-ray" in h or "just a moment" in h or "cdn-cgi" in h or "attention required" in h:
        return "Cloudflare"
    if "incapsula" in h or "_incapsula_resource" in h or "request unsuccessful" in h:
        return "Incapsula/Imperva"
    if "akamai" in h or "reference&#32;#" in h or "access denied" in h:
        return "Akamai/AccessDenied"
    if "error code 5" in h or "503 service" in h:
        return "generic 5xx"
    return None


def extract_text(html):
    soup = BeautifulSoup(html, "html.parser")
    for t in soup(["script", "style", "noscript", "nav", "footer", "header", "svg"]):
        t.decompose()
    return soup.get_text(" ", strip=True)


def find_links(html, base, sel):
    soup = BeautifulSoup(html, "html.parser")
    out, seen = [], set()
    for a in soup.select(sel):
        href = (a.get("href") or "").strip()
        if not href:
            continue
        absu = requests.compat.urljoin(base, href)
        if absu in seen:
            continue
        seen.add(absu)
        out.append((absu, re.sub(r"\s+", " ", a.get_text(" ", strip=True))[:70]))
    return out


# ── transports: each returns (status:int|None, html:str, note:str) ───────────
def t_plain(url):
    r = requests.get(url, headers=HEADERS, timeout=25)
    return r.status_code, r.text, ""

def t_curl_cffi(url):
    last = None
    for tgt in ("chrome", "chrome124", "chrome120"):
        try:
            r = cffi.get(url, headers=HEADERS, timeout=25, impersonate=tgt)
            return r.status_code, r.text, f"impersonate={tgt}"
        except Exception as e:
            last = e
    raise last

def t_cloudscraper(url):
    scraper = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "windows", "mobile": False})
    r = scraper.get(url, timeout=40)
    return r.status_code, r.text, ""

def t_proxy(url):
    if not CF_WORKER_URL:
        raise RuntimeError("CF_WORKER_URL not set (no secret)")
    r = requests.get(CF_WORKER_URL, params={"url": url},
                     headers={"X-Proxy-Token": CF_WORKER_TOKEN}, timeout=40)
    return r.status_code, r.text, "via CF worker"

def t_jina(url):
    hdrs = {"X-Return-Format": "html", "User-Agent": UA}
    if JINA_API_KEY:
        hdrs["Authorization"] = f"Bearer {JINA_API_KEY}"
    r = requests.get(f"https://r.jina.ai/{url}", headers=hdrs, timeout=70)
    return r.status_code, r.text, "keyless" if not JINA_API_KEY else "keyed"

def t_playwright(url):
    if not HAVE_PW:
        raise RuntimeError("playwright not installed")
    note = "stealth" if stealth_sync else "no-stealth"
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
        page = browser.new_page(user_agent=UA, locale="de-DE")
        if stealth_sync:
            try:
                stealth_sync(page)
            except Exception:
                note = "stealth-failed"
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=35000)
            page.wait_for_timeout(6000)  # let any JS challenge resolve
            try:
                page.wait_for_selector(".views-row", timeout=8000)
            except Exception:
                pass
            html = page.content()
            status = 200
        finally:
            browser.close()
    return status, html, note


TRANSPORTS = [("plain", t_plain, True),
              ("curl_cffi", t_curl_cffi, HAVE_CFFI),
              ("cloudscraper", t_cloudscraper, HAVE_CS),
              ("cf_proxy", t_proxy, True),          # raises cleanly if no secret
              ("jina", t_jina, True),
              ("playwright_stealth", t_playwright, HAVE_PW)]


def analyse(html, base):
    """Return dict summarising one HTML body; prints selector hits."""
    bk = block_kind(html)
    txt = extract_text(html)
    print(f"      bytes={len(html)}  text={len(txt)}  block={bk or 'none'}")
    if bk:
        print(f"      body[:300]={html[:300]!r}")
        return {"blocked": True, "links": 0, "no_vac": False}
    nv = next((p for p in NO_VACANCY if p in txt.lower()), None)
    if nv:
        print(f"      ℹ️ no-vacancy phrase: {nv!r}")
    best = 0
    for sel in SELECTORS:
        links = find_links(html, base, sel)
        tag = "  <-- jobs selector" if sel == SELECTORS[0] else ""
        print(f"      select {sel!r}: {len(links)}{tag}")
        for u, t in links[:6]:
            print(f"          • {t or '(no text)'}  ->  {u}")
        if sel == SELECTORS[0]:
            best = len(links)
    return {"blocked": False, "links": best, "no_vac": bool(nv)}


def run(url, label):
    print(f"\n{'='*74}\n{label}: {url}\n{'='*74}")
    summary = []
    winner = None
    for name, fn, available in TRANSPORTS:
        print(f"  -- {name} --")
        if not available:
            print("      (not installed — skipped)")
            summary.append((name, "skipped"))
            continue
        try:
            t0 = time.time()
            status, html, note = fn(url)
            dt = time.time() - t0
            ok = status == 200 and len(html) > 600 and not block_kind(html)
            print(f"      HTTP {status}  ({dt:.1f}s)  {note}")
            res = analyse(html, url)
            verdict = "OK" if (ok and not res["blocked"]) else ("blocked" if res["blocked"] else f"HTTP {status}")
            summary.append((name, verdict + (f", {res['links']} jobs" if not res["blocked"] else "")))
            if ok and not res["blocked"] and winner is None and (res["links"] > 0 or res["no_vac"]):
                winner = name
        except Exception as e:
            print(f"      ERROR {type(e).__name__}: {str(e)[:140]}")
            summary.append((name, f"error: {type(e).__name__}"))
        time.sleep(0.8)
    return summary, winner


def main():
    print("diag_boell.py")
    print(f"  curl_cffi={HAVE_CFFI}  cloudscraper={HAVE_CS}  playwright={HAVE_PW}  "
          f"stealth={'yes' if stealth_sync else 'no'}")
    print(f"  CF proxy: {'configured' if CF_WORKER_URL else 'NOT set'}  "
          f"jina key: {'set' if JINA_API_KEY else 'keyless'}")

    s_list, win_list = run(LISTING, "LISTING")
    s_det, win_det = run(DETAIL, "DETAIL PAGE")

    print(f"\n{'='*74}\nSUMMARY\n{'='*74}")
    print("  Listing transports:")
    for n, v in s_list:
        print(f"    {n:20} {v}")
    print("  Detail transports:")
    for n, v in s_det:
        print(f"    {n:20} {v}")
    print()
    if win_list:
        cfg = {"plain": "(no flags needed)",
               "curl_cffi": '"tls_impersonate": true',
               "cloudscraper": '"use_cloudscraper": true',
               "cf_proxy": '"proxy": "cloudflare_worker"',
               "jina": '"proxy": "jina"',
               "playwright_stealth": 'method "playwright" (+ stealth)'}.get(win_list, win_list)
        print(f"  ✅ First working transport for the listing: {win_list}  ->  config: {cfg}")
        print(f"     Jobs selector to use: {SELECTORS[0]}")
    else:
        print("  ❌ No transport returned the real listing. Böll likely ASN-blocks the runner")
        print("     on every channel we control — candidate for 'drop it' unless jina/playwright above worked.")


if __name__ == "__main__":
    main()

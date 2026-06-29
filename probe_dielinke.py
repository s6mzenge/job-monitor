"""
probe_dielinke.py — find out, FROM THE AZURE RUNNER, which fetch transport (if
any) can actually retrieve the Die Linke party careers page (config-de.json id
223), which has been HTTP-429'd on the first request of every run since
2026-06-28 on both the direct egress and the Cloudflare Worker fallback.

It tries every transport in one shot and prints a comparison + a ready-to-paste
config block for the winner. Read-only: no state writes, no Telegram, no LLM.

Why it must run on the runner, not locally:
    The 429 is datacenter/ASN-reputation throttling. From your residential IP
    you'll almost certainly get 200 and learn nothing about the Azure block.
    Run it via the "Probe Die Linke" workflow (workflow_dispatch).

Design note:
    Like diag_site.py, this imports monitor.py and reuses its REAL primitives
    (_SESSION, HEADERS, worker creds, curl_cffi, the _pw_launch/stealth recipe,
    and the .content extraction + challenge/no-vacancy guards), so a "works
    here" result transfers directly to production with zero handler drift.
"""

import os
import sys
import types

# ── Satisfy monitor.py's import-time reads (it does os.environ[...] for these
#    three and load a config file). We never use them — the probe only needs
#    them present so the import doesn't KeyError. Identical to diag_site.py. ──
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "probe")
os.environ.setdefault("TELEGRAM_CHAT_ID", "probe")
os.environ.setdefault("ANTHROPIC_API_KEY", "probe")
os.environ.setdefault("CONFIG_FILE", "config-de.json")  # 223 lives in the DE lane


# ── Only stub heavy deps if they're genuinely missing (bare box / local run).
#    On the runner requirements.txt is installed, so these never trigger and the
#    real curl_cffi / playwright are used. ──
def _ensure(name, builder):
    try:
        __import__(name)
    except Exception:
        builder()


_ensure("cloudscraper", lambda: sys.modules.__setitem__(
    "cloudscraper", types.ModuleType("cloudscraper")))


def _stub_pw():
    pw = types.ModuleType("playwright")
    sync = types.ModuleType("playwright.sync_api")
    sync.sync_playwright = lambda *a, **k: None
    pw.sync_api = sync
    sys.modules["playwright"] = pw
    sys.modules["playwright.sync_api"] = sync


_ensure("playwright.sync_api", _stub_pw)
_ensure("playwright_stealth", lambda: sys.modules.__setitem__(
    "playwright_stealth", types.ModuleType("playwright_stealth")))

import monitor as M  # noqa: E402
from bs4 import BeautifulSoup  # noqa: E402

# Defensive: make sure nothing can reach out even if a code path tries to.
M.evaluate_with_anthropic = lambda *a, **k: None
M.send_telegram = lambda *a, **k: None


URL = os.environ.get("PROBE_URL", "https://www.die-linke.de/partei/jobs/")
SELECTOR = os.environ.get("PROBE_SELECTOR", ".content")  # what id 223 extracts
HDRS_OF_INTEREST = ("server", "cf-ray", "cf-cache-status", "cf-mitigated",
                    "retry-after", "x-powered-by", "via", "x-cache",
                    "x-ratelimit-remaining", "set-cookie")

# transport short-name -> (ok, status, chars) — filled in as we go
R = {}


def _clean(s, n=200):
    return " ".join((s or "").split())[:n]


def _analyse(short, label, status, text, headers, err):
    """Print one transport's outcome and record (ok, status, chars)."""
    print(f"\n=== {label} ===")
    if err is not None:
        print(f"  RESULT: EXCEPTION — {type(err).__name__}: {err}")
        R[short] = (False, None, 0)
        return

    body = text or ""
    print(f"  HTTP {status}   bytes={len(body)}")

    # Response headers that explain a 429 / reveal the WAF in front of the origin
    hdr = {str(k).lower(): v for k, v in (headers or {}).items()}
    for k in HDRS_OF_INTEREST:
        if k in hdr:
            print(f"    {k}: {_clean(str(hdr[k]), 120)}")

    # What the PRODUCTION hash path would actually capture. The '.content'
    # selector is what config-de.json[223] uses; on jina's plain-text body it
    # won't match, but extract_text's for-else falls back to whole-page text —
    # so we report both so the result is unambiguous either way.
    sel_text = M.extract_text(BeautifulSoup(body, "html.parser"), SELECTOR)
    full_text = M.extract_text(BeautifulSoup(body, "html.parser"), "")
    low = full_text.lower()
    is_challenge = len(full_text) < 600 and any(p in low for p in M.CHALLENGE_PHRASES)
    is_novac = any(p in low for p in M.NO_VACANCY_PHRASES)
    print(f"    extract('{SELECTOR}')={len(sel_text)} chars   "
          f"extract(whole)={len(full_text)} chars")
    print(f"    challenge_page={is_challenge}   no_vacancy_phrase={is_novac}")
    print(f"    snippet: {_clean(full_text, 220)}")

    ok = (status == 200) and (len(full_text) >= 50) and not is_challenge
    print(f"    → {'✅ USABLE' if ok else '⛔ not usable'}")
    R[short] = (ok, status, len(full_text))


# ── egress IP (so you can compare with a residential `curl` of the same URL) ──
print("=" * 70)
print(f"PROBE TARGET: {URL}")
try:
    ip = M._SESSION.get("https://api.ipify.org", timeout=8).text.strip()
    print(f"Runner egress IP: {ip}  (datacenter — compare vs your home IP)")
except Exception as e:
    print(f"Runner egress IP: (couldn't determine: {e})")
print("=" * 70)


# ── 1. plain — direct from the Azure runner IP (production primary for id 223) ──
try:
    r = M._SESSION.get(URL, headers=M.HEADERS, timeout=M.HTTP_TIMEOUT)
    _analyse("plain", "plain  (Azure runner IP — current primary)",
             r.status_code, r.text, r.headers, None)
except Exception as e:
    _analyse("plain", "plain  (Azure runner IP — current primary)", None, None, None, e)


# ── 2. curl_cffi TLS impersonation — real Chrome JA3, still the Azure IP ──
if M.HAVE_CURL_CFFI:
    try:
        r = M.cffi_requests.get(URL, impersonate="chrome", timeout=25)
        _analyse("tls", "tls / curl_cffi  (Azure IP, real Chrome JA3)",
                 r.status_code, r.text, dict(r.headers), None)
    except Exception as e:
        _analyse("tls", "tls / curl_cffi  (Azure IP, real Chrome JA3)", None, None, None, e)
else:
    print("\n=== tls / curl_cffi ===  SKIPPED (curl_cffi unavailable)")
    R["tls"] = (False, None, 0)


# ── 3. Cloudflare Worker proxy — egress from a CF edge IP (production fallback) ──
if M.CF_WORKER_URL:
    try:
        r = M._SESSION.get(M.CF_WORKER_URL, params={"url": URL},
                           headers={"X-Proxy-Token": M.CF_WORKER_TOKEN},
                           timeout=M.HTTP_TIMEOUT)
        _analyse("worker", "cloudflare worker  (CF edge IP — current fallback)",
                 r.status_code, r.text, r.headers, None)
    except Exception as e:
        _analyse("worker", "cloudflare worker  (CF edge IP — current fallback)", None, None, None, e)
else:
    print("\n=== cloudflare worker ===  SKIPPED (CF_WORKER_URL unset)")
    R["worker"] = (False, None, 0)


# ── 4. r.jina.ai — egress from Jina's own IPs + JS render (the off-cloud rung) ──
try:
    jhdrs = {"X-Return-Format": "text",
             "User-Agent": M.HEADERS.get("User-Agent", "Mozilla/5.0")}
    if M.JINA_API_KEY:
        jhdrs["Authorization"] = f"Bearer {M.JINA_API_KEY}"
    r = M._SESSION.get(f"https://r.jina.ai/{URL}", headers=jhdrs, timeout=M.JINA_TIMEOUT)
    _analyse("jina", "jina r.jina.ai  (Jina IP, rendered text)",
             r.status_code, r.text, r.headers, None)
except Exception as e:
    _analyse("jina", "jina r.jina.ai  (Jina IP, rendered text)", None, None, None, e)


# ── 5. Playwright headless Chromium + stealth — real browser, still Azure IP ──
#    Uses monitor.py's exact launch recipe so it reflects production capability.
try:
    from playwright.sync_api import sync_playwright
    mode = getattr(M, "_STEALTH_MODE", None)
    pw_cm = (M.Stealth().use_sync(sync_playwright())
             if mode == "context" else sync_playwright())
    status, html = None, ""
    with pw_cm as p:
        b = M._pw_launch(p)
        ctx = b.new_context()
        page = ctx.new_page()
        if mode == "page":
            M._stealth_sync(page)
        resp = page.goto(URL, timeout=30000, wait_until="domcontentloaded")
        page.wait_for_timeout(2000)
        status = resp.status if resp else 200
        html = page.content()
        try:
            rhdrs = resp.headers if resp else {}
        except Exception:
            rhdrs = {}
        b.close()
    _analyse("playwright", "playwright  (Azure IP, headless Chromium + stealth)",
             status, html, rhdrs, None)
except Exception as e:
    _analyse("playwright", "playwright  (Azure IP, headless Chromium + stealth)", None, None, None, e)


# ════════════════════ SUMMARY + RECOMMENDED CONFIG ════════════════════
def _ok(name):
    return R.get(name, (False, None, 0))[0]


print("\n\n" + "=" * 70)
print("SUMMARY — single hit per transport (no retries)")
print("=" * 70)
print(f"  {'transport':32} {'usable':8} {'status':>7} {'chars':>8}")
for short, label in [("plain", "plain (Azure IP)"),
                     ("tls", "tls / curl_cffi (Azure IP)"),
                     ("worker", "cloudflare worker (CF IP)"),
                     ("jina", "jina r.jina.ai (Jina IP)"),
                     ("playwright", "playwright (Azure IP)")]:
    ok, st, ch = R.get(short, (False, "—", 0))
    print(f"  {('✅' if ok else '⛔')} {label:30} {str(ok):8} {str(st):>7} {ch:>8}")

# Map single-hit results onto the configs you'd actually ship. plain/tls both
# auto-fall-back to the worker in fetch_page, so their shipped verdict is OR'd
# with the worker result.
print("\nWhat this means for config-de.json id 223:")
shippable = [
    ("no change (plain → worker fallback)", _ok("plain") or _ok("worker"), None),
    ('"tls_impersonate": true (tls → worker fallback)', _ok("tls") or _ok("worker"),
     ('"tls_impersonate"', "true")),
    ('"proxy": "cloudflare_worker"', _ok("worker"), ('"proxy"', '"cloudflare_worker"')),
    ('"proxy": "jina"', _ok("jina"), ('"proxy"', '"jina"')),
]
for desc, ok, _kv in shippable:
    print(f"  {('✅ WORKS' if ok else '⛔ blocked')}: {desc}")
if _ok("playwright"):
    print('  ✅ WORKS: switch "method" to "playwright" (heavier — needs a browser per run)')

# Pick the simplest winner and print a paste-ready block.
winner_key = None
for desc, ok, kv in shippable:
    if ok:
        winner_key = kv  # None for "no change"
        winner_desc = desc
        break

print("\n" + "-" * 70)
if winner_key is None and any(_ok(t) for t in ("plain", "tls", "worker", "jina")):
    print(f"RECOMMENDATION: {winner_desc} — id 223 should self-heal, no edit needed.")
elif winner_key is not None:
    k, v = winner_key
    block = f'''  {{
    "id": 223,
    "name": "Die Linke",
    "url": "https://www.die-linke.de/partei/jobs/",
    "method": "html",
    "selector": ".content",
    "link_selector": "",
    "base_url": "https://www.die-linke.de",
    "skip_no_vacancy_check": true,
    {k}: {v}
  }}'''
    print(f"RECOMMENDATION: {winner_desc}")
    print("Paste this over the id-223 block in config-de.json:\n")
    print(block)
elif _ok("playwright"):
    print('RECOMMENDATION: only Playwright got through. Change id 223 to '
          '"method": "playwright" (optionally add "stealth": true). '
          "Heavier than HTTP — weigh it against the value of this single source.")
else:
    print("RECOMMENDATION: every transport is blocked from cloud egress. "
          "Either drop id 223 (you already cover id 154 dielinkebt.de, the more "
          "policy-relevant Linke page), or add a residential/rotating proxy rung. "
          "Confirm with a residential `curl -i` of the URL: if your home IP gets "
          "200, this is pure datacenter-ASN blocking.")
print("-" * 70)

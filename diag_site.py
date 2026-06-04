"""
diag_site.py — run ONE site through the real monitor.py handlers, locally,
with no LLM calls, no Telegram, and no state writes.

Usage (Windows PowerShell):
    python diag_site.py "Reprieve"
    python diag_site.py "ODI Global"
    python diag_site.py "Anthropic"

Why this exists:
    It imports monitor.py and calls the exact same handler the live pipeline
    uses for that site, so there's zero drift between what you test and what
    runs in GitHub Actions. Use it to (a) confirm a config fix before
    committing, and (b) investigate "silent zero" sites that never error but
    never surface a job (selector matches nothing vs. genuinely no vacancies).

Proxy sites:
    For sites with "proxy": "cloudflare_worker", set CF_WORKER_URL in your
    environment (or you'll be prompted). The token is read from CF_WORKER_TOKEN
    or prompted via getpass — it is never echoed; only its length is shown.
"""

import os
import sys
import types
import getpass

# ── Stub secrets the module reads at import time (we patch out anything
#    that would actually call out before we use it). ──
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "diag")
os.environ.setdefault("TELEGRAM_CHAT_ID", "diag")
os.environ.setdefault("ANTHROPIC_API_KEY", "diag")

# ── Stub optional heavy deps only if they're not installed. On your machine
#    cloudscraper/playwright ARE installed (requirements.txt), so these stubs
#    won't trigger — they only keep the import from crashing on a bare box. ──
def _ensure(name, builder):
    try:
        __import__(name)
    except Exception:
        builder()

_ensure("cloudscraper", lambda: sys.modules.__setitem__(
    "cloudscraper", types.ModuleType("cloudscraper")))
def _stub_pw():
    pw = types.ModuleType("playwright"); sync = types.ModuleType("playwright.sync_api")
    sync.sync_playwright = lambda *a, **k: None; pw.sync_api = sync
    sys.modules["playwright"] = pw; sys.modules["playwright.sync_api"] = sync
_ensure("playwright.sync_api", _stub_pw)
_ensure("playwright_stealth", lambda: sys.modules.__setitem__(
    "playwright_stealth", types.ModuleType("playwright_stealth")))

import monitor  # noqa: E402

# ── Neutralise side effects ──
monitor.evaluate_with_anthropic = lambda *a, **k: None
monitor.send_telegram = lambda *a, **k: None


def main():
    if len(sys.argv) < 2:
        print('Usage: python diag_site.py "<Site Name>"')
        print("\nConfigured sites:")
        for s in monitor.SITES:
            print(f"  [{s.get('id')}] {s['name']}  ({s['method']})")
        return

    name = sys.argv[1].strip().lower()
    site = next((s for s in monitor.SITES if s["name"].strip().lower() == name), None)
    if not site:
        print(f"No site named {sys.argv[1]!r}. Run with no args to list sites.")
        return

    print(f"=== {site['name']}  (id {site.get('id')}, method {site['method']}) ===")
    print(f"URL: {site['url']}")

    # Worker proxy credentials, if needed
    if site.get("proxy") == "cloudflare_worker":
        url = os.environ.get("CF_WORKER_URL", "") or input("CF_WORKER_URL: ").strip()
        tok = os.environ.get("CF_WORKER_TOKEN", "")
        if not tok:
            tok = getpass.getpass("CF_WORKER_TOKEN (hidden): ").strip()
        monitor.CF_WORKER_URL = url
        monitor.CF_WORKER_TOKEN = tok
        print(f"  proxy: cloudflare_worker  (worker set: {bool(url)}, token chars: {len(tok)})")

    handler = monitor.METHOD_HANDLERS.get(site["method"])
    if not handler:
        print(f"No handler for method {site['method']!r}")
        return

    # seen_urls = empty set -> everything is reported as "new"
    result = handler(site, set())
    err = monitor._consume_error()

    print("\n--- RESULT ---")
    if result is None:
        print(f"Handler returned None (FAILURE). Captured error:\n  {err or '(none)'}")
        return

    if isinstance(result, dict) and result.get("type") == "hash_check":
        text = result.get("text", "")
        print(f"Mode: HASH-CHECK (no link_selector)")
        print(f"Extracted content: {len(text)} chars")
        print(f"Headings found: {result.get('titles', [])}")
        low = text.lower()
        hit = [p for p in monitor.NO_VACANCY_PHRASES if p in low]
        print(f"'No vacancy' phrase match: {hit or 'none'}")
        print("\n--- first 1500 chars of extracted text ---")
        print(text[:1500])
        return

    total = result.get("total")
    new = result.get("new", [])
    print(f"Mode: JOB-LIST")
    print(f"total found: {total}   new (would be evaluated): {len(new)}")
    for j in new[:15]:
        print(f"  • {j['title']}")
        print(f"      {j['url']}")
    if new:
        print("\n--- detail_text of first job (first 1200 chars) ---")
        print((new[0].get("detail_text") or "")[:1200])
    else:
        print("\n(No new jobs. If total is also 0, the source genuinely lists none,")
        print(" or the selector/filter is matching nothing — compare against the live page.)")


if __name__ == "__main__":
    main()

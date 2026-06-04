import json
import os
import hashlib
import re
import requests
import time
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
import cloudscraper
from playwright.sync_api import sync_playwright
from report import save_report
import issues
try:
    from playwright_stealth import stealth_sync as _stealth_sync
    _STEALTH_MODE = "page"
except ImportError:
    try:
        from playwright_stealth import Stealth
        _STEALTH_MODE = "context"
    except ImportError:
        _STEALTH_MODE = None

# ─── Load configuration ───
with open("config.json", "r", encoding="utf-8") as f:
    config = json.load(f)

QUALIFICATIONS = config["qualifications"]
SITES = config["sites"]

# ─── Load CV ───
CV_FILE = "cv.txt"
if os.path.exists(CV_FILE):
    with open(CV_FILE, "r", encoding="utf-8") as f:
        CANDIDATE_CV = f.read().strip()
    print(f"Loaded CV from {CV_FILE} ({len(CANDIDATE_CV)} chars)")
else:
    CANDIDATE_CV = QUALIFICATIONS
    print(f"⚠️ {CV_FILE} not found — falling back to qualifications summary")

# ─── Secrets from environment ───
TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-7")
DRY_RUN_FILE = "dry_run.txt"
if os.path.exists(DRY_RUN_FILE):
    with open(DRY_RUN_FILE, "r") as f:
        DRY_RUN = f.read().strip().lower() == "true"
else:
    DRY_RUN = False
CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "")
CF_WORKER_TOKEN = os.environ.get("CF_WORKER_TOKEN", "")


# ─── Anthropic rate limiter ───
# Tier 1 limits for Claude Opus 4.7: 50 RPM, ~30K ITPM, ~8K OTPM.
# At ~3K input tokens per call the ITPM is the real bottleneck (~10 calls/min).
# We cap at 9 RPM to stay safely under the per-minute input-token ceiling.
_anthropic_calls = []

def anthropic_rate_limit():
    """Enforce max 9 calls per 60 seconds (staying under Opus 4.7 ITPM ceiling)."""
    global _anthropic_calls
    now_ts = time.time()
    _anthropic_calls = [t for t in _anthropic_calls if now_ts - t < 60]
    if len(_anthropic_calls) >= 9:
        wait = 60 - (now_ts - _anthropic_calls[0]) + 1
        print(f"    ⏳ Anthropic rate limit — waiting {wait:.0f}s")
        time.sleep(wait)
    _anthropic_calls.append(time.time())

# ─── Error capture for issues log ───
# Handlers swallow exceptions internally and return None on failure, so the
# actual error message is otherwise lost. _record_error() is called inside
# each exception block; the main loop calls _consume_error() after every
# handler call to read and clear the captured message.
#
# IMPORTANT: error messages from libraries like `requests` often include the
# full request URL, which can contain API keys as query parameters. We redact
# common secret patterns before storing so they never reach issues.json (which
# is committed to the public repo).
_last_error = ""

# Patterns: key/token/secret in URL query params, Google API keys, Bearer tokens
_REDACT_KV = re.compile(
    r'(\b(?:key|token|api[_-]?key|auth[_-]?token|password|secret|access[_-]?token)=)[^&\s"\'<>]+',
    re.IGNORECASE,
)
_REDACT_GOOGLE_KEY = re.compile(r'AIza[0-9A-Za-z\-_]{35}')
_REDACT_BEARER = re.compile(r'(Bearer\s+)[A-Za-z0-9\-_\.]+', re.IGNORECASE)

def _redact_secrets(text):
    """Strip common secret patterns from a string before logging."""
    if not text:
        return text
    out = _REDACT_KV.sub(r'\1***', text)
    out = _REDACT_GOOGLE_KEY.sub('AIza***REDACTED***', out)
    out = _REDACT_BEARER.sub(r'\1***', out)
    return out

def _record_error(msg):
    global _last_error
    _last_error = _redact_secrets(msg)

def _consume_error():
    global _last_error
    msg = _last_error
    _last_error = ""
    return msg


# ─── State management ───
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Cloudflare WAF on several sites (HIS Hamburg, IfG, PHF, twentyfifty,
    # Ceasefire) returns HTTP 415 when the requests-default Accept header is
    # sent. Mirror a real browser to avoid the filter.
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,"
              "image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
}

NO_VACANCY_PHRASES = [
    "no current vacancies",
    "no vacancies",
    "no positions available",
    "no openings",
    "check back later",
    "check back at a later date",
    "currently no vacancies",
    "no current opportunities",
    "no open positions",
    "no jobs are available",
    "no items found",
    "keine offenen positionen",
    "derzeit keine",
    "do not have any vacancies",
    "not currently recruiting",
    "do not accept unsolicited",
    "no posts on the list",
]

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ═══════════════════════════════════════════════════════════════
#  UTILITY FUNCTIONS
# ═══════════════════════════════════════════════════════════════

# Retryable HTTP status codes: rate limiting + transient 5xx (incl. Cloudflare
# 521/522/523/524 which generally mean "origin temporarily unreachable").
# Note: 403/404 are NOT retried — those are persistent and need a config fix.
RETRY_STATUS_CODES = {429, 500, 502, 503, 504, 521, 522, 523, 524}
FETCH_MAX_ATTEMPTS = 3
FETCH_RETRY_BACKOFF = 10  # seconds between retries

def fetch_page(url, extra_headers=None, proxy=None):
    """Fetch a URL and return a BeautifulSoup object, or None on error.

    Retries up to FETCH_MAX_ATTEMPTS times on transient errors:
      - HTTP 429 (rate-limited) and 5xx (server errors, incl. Cloudflare 52x)
      - Network timeouts and connection errors
    Permanent errors (403, 404, etc.) fail fast on the first attempt.
    """
    hdrs = {**HEADERS, **(extra_headers or {})}
    last_err = None
    for attempt in range(1, FETCH_MAX_ATTEMPTS + 1):
        try:
            if proxy == "cloudflare_worker" and CF_WORKER_URL:
                resp = requests.get(
                    CF_WORKER_URL,
                    params={"url": url},
                    headers={"X-Proxy-Token": CF_WORKER_TOKEN},
                    timeout=30,
                )
            else:
                resp = requests.get(url, headers=hdrs, timeout=30)

            # Retryable status codes: log and try again
            if resp.status_code in RETRY_STATUS_CODES and attempt < FETCH_MAX_ATTEMPTS:
                print(
                    f"    HTTP {resp.status_code} for {url} — retry in "
                    f"{FETCH_RETRY_BACKOFF}s (attempt {attempt}/{FETCH_MAX_ATTEMPTS})"
                )
                last_err = f"HTTP {resp.status_code}"
                time.sleep(FETCH_RETRY_BACKOFF)
                continue

            resp.raise_for_status()
            return BeautifulSoup(resp.text, "html.parser")
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = e
            if attempt < FETCH_MAX_ATTEMPTS:
                print(
                    f"    Network error for {url} — retry in "
                    f"{FETCH_RETRY_BACKOFF}s (attempt {attempt}/{FETCH_MAX_ATTEMPTS}): {e}"
                )
                time.sleep(FETCH_RETRY_BACKOFF)
                continue
            break
        except Exception as e:
            # Permanent error (HTTPError for 4xx, parse error, etc.) — fail fast
            last_err = e
            break

    _record_error(f"fetch_page: {last_err}")
    print(f"    Error fetching {url}: {last_err}")
    return None


def request_with_retry(method, url, **kwargs):
    """Like requests.request(), but retries transient failures.

    Used by the JSON/XML/RSS API handlers, which previously called
    requests.get/post directly with no retry — so a single transient blip
    (HTTP 429/5xx, a Cloudflare 52x, or a connection timeout from a GHA
    runner's datacenter IP) killed the whole site for that run. fetch_page
    already had this resilience for HTML sites; this brings the API handlers
    to parity.

    Retry policy mirrors fetch_page exactly:
      - HTTP 429 + 5xx (incl. Cloudflare 521-524): retry with backoff
      - Timeouts / connection errors: retry with backoff
      - Permanent errors (403, 404, other 4xx, parse errors): fail fast
    Returns the Response object WITHOUT calling raise_for_status(), so the
    caller keeps its existing raise_for_status()/.json()/try-except flow
    unchanged (its except block still records the error message). On a final
    transient failure the underlying exception is re-raised for the same
    reason.
    """
    last_exc = None
    for attempt in range(1, FETCH_MAX_ATTEMPTS + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code in RETRY_STATUS_CODES and attempt < FETCH_MAX_ATTEMPTS:
                print(
                    f"    HTTP {resp.status_code} for {url} — retry in "
                    f"{FETCH_RETRY_BACKOFF}s (attempt {attempt}/{FETCH_MAX_ATTEMPTS})"
                )
                time.sleep(FETCH_RETRY_BACKOFF)
                continue
            return resp
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_exc = e
            if attempt < FETCH_MAX_ATTEMPTS:
                print(
                    f"    Network error for {url} — retry in "
                    f"{FETCH_RETRY_BACKOFF}s (attempt {attempt}/{FETCH_MAX_ATTEMPTS}): {e}"
                )
                time.sleep(FETCH_RETRY_BACKOFF)
                continue
            raise
        except Exception:
            # Permanent / non-retryable — let the caller's except handle it.
            raise
    # Exhausted retries on a retryable connection error.
    if last_exc:
        raise last_exc
    return resp


def extract_text(soup, selector=""):
    """Extract cleaned text from a BeautifulSoup object, optionally within a CSS selector."""
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
        tag.decompose()
    for sel in ["[class*='cookie']", "[id*='cookie']", "[class*='consent']", "[id*='consent']"]:
        for tag in soup.select(sel):
            tag.decompose()
    if selector:
        for sel in selector.split(","):
            target = soup.select_one(sel.strip())
            if target:
                text = target.get_text(separator="\n", strip=True)
                break
        else:
            text = soup.get_text(separator="\n", strip=True)
    else:
        text = soup.get_text(separator="\n", strip=True)
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return "\n".join(lines)

def get_nested(obj, dotted_key, default=""):
    """Safely get a nested value from a dict using dot notation, e.g. 'location.city'."""
    keys = dotted_key.split(".")
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k, default)
        else:
            return default
    return obj


# ═══════════════════════════════════════════════════════════════
#  METHOD HANDLERS — each returns:
#    None                              → error / could not fetch
#    {"type": "hash_check", ...}       → content-hash sites
#    {"total": int, "new": list}       → normal job list
# ═══════════════════════════════════════════════════════════════

# ─── 1. HTML SCRAPING ───
def check_html(site, seen_urls):
    """Scrape a standard HTML careers page for job links, then fetch detail pages."""
    listing_url = site["url"]
    link_selector = site.get("link_selector", "")
    base_url = site.get("base_url", "")
    selector = site.get("selector", "")
    location_filter = site.get("location_filter", "")

    if site.get("use_cloudscraper"):
        scraper = cloudscraper.create_scraper()
        try:
            resp = scraper.get(listing_url, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
        except Exception as e:
            _record_error(f"cloudscraper listing: {e}")
            print(f"    Error fetching {listing_url}: {e}")
            soup = None
    else:
        soup = fetch_page(listing_url, proxy=site.get("proxy"))
    if not soup:
        return None

    if not link_selector:
        text = extract_text(soup, selector)
        if len(text) < 50 and not any(p in text.lower() for p in NO_VACANCY_PHRASES):
            print(f"    ⚠️ Very little content extracted ({len(text)} chars) — site may require JavaScript rendering")
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        titles = []
        target_el = soup.select_one(selector) if selector else soup
        if target_el:
            for h in target_el.select("h2, h3, h4, h5"):
                h_text = h.get_text(strip=True)
                if h_text and len(h_text) < 100:
                    titles.append(h_text)
        return {"type": "hash_check", "text": text, "hash": text_hash, "titles": titles}


    if location_filter:
        anchors = []
        for section in soup.select("section.openings-section"):
            header = section.select_one("header, .opening-header")
            if header and location_filter.lower() in header.get_text().lower():
                anchors.extend(section.select(link_selector))
        print(f"    Filtered to {len(anchors)} links in '{location_filter}' sections")
    else:
        if site.get("scope_links") and selector:
            scope_el = soup.select_one(selector.split(",")[0].strip())
            anchors = scope_el.select(link_selector) if scope_el else []
        else:
            anchors = soup.select(link_selector)
    if not anchors:
        check_text = extract_text(soup, selector)
        if len(check_text) < 100 and not any(p in check_text.lower() for p in NO_VACANCY_PHRASES):
            print(f"    ⚠️ No job links found and page content is minimal — likely JS-rendered")

    all_urls = set()
    jobs = []
    seen_in_batch = set()
    for a in anchors:
        href = a.get("href", "").strip()
        if not href or href == "#" or href == listing_url:
            continue

        title = a.get_text(strip=True) or "Untitled"
        # If the link wraps a card, prefer the heading inside it
        inner_heading = a.select_one("h2, h3, h4, h5")
        if inner_heading:
            title = inner_heading.get_text(strip=True)
        if title.lower() in ("view job", "apply", "apply now", "learn more",
                              "read more", "click here", "view"):
            card = None
            node = a.parent
            card_keywords = ("item", "card", "career", "listing", "posting", "vacancy")
            while node and node.name:
                classes = " ".join(node.get("class", []))
                role = node.get("role", "")
                if any(kw in classes.lower() for kw in card_keywords) or role == "listitem":
                    card = node
                    break
                if node.name in ("article", "li"):
                    card = node
                    break
                node = node.parent

            if card:
                heading = card.select_one(
                    "[class*='subheadline'], [class*='headline'], [class*='heading'], h2, h3, h4, h5"
                )
                if heading:
                    title = heading.get_text(strip=True)
                else:
                    continue
            else:
                continue

        full_url = urljoin(base_url + "/", href) if base_url else urljoin(listing_url, href)
        full_url = full_url.rstrip("/")

        all_urls.add(full_url)

        if full_url not in seen_urls and full_url not in seen_in_batch:
            jobs.append({"title": title, "url": full_url})
            seen_in_batch.add(full_url)

    new_jobs = []
    for job in jobs:
        if site.get("skip_detail_fetch"):
            detail_text = f"Title: {job['title']}"
        else:
            time.sleep(1)
            if site.get("use_cloudscraper"):
                try:
                    dr = scraper.get(job["url"], timeout=30)
                    dr.raise_for_status()
                    detail_soup = BeautifulSoup(dr.text, "html.parser")
                except Exception as e:
                    print(f"    Error fetching detail {job['url']}: {e}")
                    detail_soup = None
            else:
                detail_soup = fetch_page(job["url"], proxy=site.get("proxy"))
            detail_text = extract_text(detail_soup) if detail_soup else ""
        new_jobs.append({
            "title": job["title"],
            "url": job["url"],
            "detail_text": detail_text or f"Title: {job['title']} (detail page could not be loaded)"
        })
    
    return {"total": len(all_urls), "new": new_jobs}


# ─── 2. WORKDAY API ───
def check_workday(site, seen_urls):
    """Query Workday's public JSON API for job listings, with pagination."""
    api = site["api"]
    fields = api["job_fields"]
    base_url = site["base_url"]
    detail_api_template = site.get("job_detail_api", "")

    all_postings = []
    offset = 0
    limit = api["body"].get("limit", 20)

    while True:
        body = {**api["body"], "offset": offset, "limit": limit}
        try:
            resp = request_with_retry("POST", api["url"], headers=api["headers"], json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
            _record_error(f"Workday API: {e}")
            print(f"    Workday API error: {e}")
            return None if not all_postings else {"total": len(all_postings), "new": []}

        postings = data.get(api["response_key"], [])
        total = data.get(api.get("total_key", "total"), 0)
        all_postings.extend(postings)

        print(f"    Fetched {len(all_postings)}/{total} jobs (offset {offset})")

        if len(all_postings) >= total or not postings:
            break
        offset += limit
        time.sleep(1)

    new_jobs = []
    for posting in all_postings:
        title = posting.get(fields["title"], "Untitled")
        path = posting.get(fields["path"], "")
        location = posting.get(fields.get("location", ""), "")
        job_url = f"{base_url}{path}" if path else ""

        if job_url in seen_urls:
            continue

        detail_text = f"Title: {title}\nLocation: {location}"
        if detail_api_template and path:
            detail_url = detail_api_template.replace("{externalPath}", path.lstrip("/"))
            try:
                time.sleep(1)
                dr = requests.get(detail_url, headers=api["headers"], timeout=30)
                dr.raise_for_status()
                dd = dr.json()
                info = dd.get("jobPostingInfo", {})
                desc_html = info.get("jobDescription", "")
                if desc_html:
                    desc_soup = BeautifulSoup(desc_html, "html.parser")
                    detail_text = f"Title: {title}\nLocation: {location}\n\n{desc_soup.get_text(separator=chr(10), strip=True)}"
            except Exception as e:
                print(f"    Could not fetch detail for {title}: {e}")

        new_jobs.append({"title": title, "url": job_url, "detail_text": detail_text})

    return {"total": len(all_postings), "new": new_jobs}


# ─── 3. GREENHOUSE API ───
def check_greenhouse(site, seen_urls):
    """Query Greenhouse's public JSON API. With ?content=true, descriptions are included."""
    api = site["api"]
    fields = api["job_fields"]
    location_filter = site.get("location_filter", "")
    department_filter = site.get("department_filter", "")

    try:
        resp = request_with_retry("GET", api["url"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Greenhouse API: {e}")
        print(f"    Greenhouse API error: {e}")
        return None

    postings = data.get(api["response_key"], [])
    print(f"    API returned {len(postings)} jobs")

    total_after_filter = 0
    new_jobs = []
    for posting in postings:
        title = posting.get(fields["title"], "Untitled")
        job_url = posting.get(fields["url"], "")
        location = get_nested(posting, fields.get("location", ""))
        desc_html = posting.get(fields.get("description_html", ""), "")
        job_id = str(posting.get(fields.get("id", ""), ""))

        # Department filter: Greenhouse returns departments as a list of objects.
        # Accepts a string OR a list of strings — matches if ANY filter value is
        # a substring of the (joined, lowercased) department names. The list form
        # exists because a single exact label is brittle: Greenhouse boards often
        # name the department something other than you'd guess (e.g. "Policy" or
        # "Public Policy & Partnerships" rather than "Public Policy"), which
        # silently filters everything out. Use the diagnostic to find the real
        # label, or list a few plausible variants here.
        if department_filter:
            departments = posting.get("departments", [])
            dept_names = " ".join(d.get("name", "") for d in departments).lower()
            wanted = department_filter if isinstance(department_filter, list) else [department_filter]
            if not any(w.lower() in dept_names for w in wanted if w):
                continue

        # Location filter
        if location_filter:
            if location_filter.lower() not in (location or "").lower():
                continue

        total_after_filter += 1

        if job_url in seen_urls or job_id in seen_urls:
            continue

        detail_text = f"Title: {title}\nLocation: {location}"
        if desc_html:
            desc_soup = BeautifulSoup(desc_html, "html.parser")
            detail_text += f"\n\n{desc_soup.get_text(separator=chr(10), strip=True)}"

        new_jobs.append({
            "title": title,
            "url": job_url or job_id,
            "detail_text": detail_text,
            "_also_track": [u for u in [job_url, job_id] if u]
        })

    if location_filter or department_filter:
        print(f"    After filtering: {total_after_filter} jobs")

    return {
        "total": total_after_filter if (location_filter or department_filter) else len(postings),
        "new": new_jobs
    }



# ─── 4. WORKABLE API ───
def check_workable(site, seen_urls):
    """Query Workable's public widget API."""
    api = site["api"]
    fields = api["job_fields"]

    try:
        resp = request_with_retry("GET", api["url"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Workable API: {e}")
        print(f"    Workable API error: {e}")
        return None

    postings = data.get(api["response_key"], [])
    print(f"    API returned {len(postings)} jobs")

    total_after_filter = 0
    new_jobs = []
    for posting in postings:
        title = posting.get(fields["title"], "Untitled")
        job_url = posting.get(fields["url"], "")
        city = get_nested(posting, fields.get("location_city", ""))
        country = get_nested(posting, fields.get("location_country", ""))
        department = posting.get(fields.get("department", ""), "")

        location_filter = site.get("location_filter", "")
        if location_filter:
            location_str = f"{city} {country}".lower()
            if location_filter.lower() not in location_str:
                continue

        total_after_filter += 1

        if job_url in seen_urls:
            continue

        detail_text = f"Title: {title}\nLocation: {city}, {country}\nDepartment: {department}"
        if job_url:
            time.sleep(1)
            detail_soup = fetch_page(job_url, proxy=site.get("proxy"))
            if detail_soup:
                page_text = extract_text(detail_soup)
                detail_text = f"Title: {title}\nLocation: {city}, {country}\n\n{page_text}"

        new_jobs.append({"title": title, "url": job_url, "detail_text": detail_text})

    return {"total": total_after_filter, "new": new_jobs}


# ─── 5. PERSONIO XML ───
def check_personio(site, seen_urls):
    """Parse Personio's XML job feed."""
    api = site["api"]
    detail_template = site.get("job_detail_url_template", "")

    try:
        resp = request_with_retry("GET", api["url"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        _record_error(f"Personio XML: {e}")
        print(f"    Personio XML error: {e}")
        return None

    positions = root.findall(f".//{api['position_element']}")
    print(f"    XML feed returned {len(positions)} positions")

    new_jobs = []
    for pos in positions:
        job_id = pos.findtext("id", "")
        title = pos.findtext("name", "Untitled")
        office = pos.findtext("office", "")
        department = pos.findtext("department", "")
        emp_type = pos.findtext("employmentType", "")
        schedule = pos.findtext("schedule", "")

        job_url = detail_template.replace("{id}", job_id) if detail_template and job_id else ""

        if job_url in seen_urls or job_id in seen_urls:
            continue

        detail_text = (
            f"Title: {title}\nOffice: {office}\nDepartment: {department}\n"
            f"Employment Type: {emp_type}\nSchedule: {schedule}"
        )

        if job_url:
            time.sleep(1)
            detail_soup = fetch_page(job_url, proxy=site.get("proxy"))
            if detail_soup:
                page_text = extract_text(detail_soup)
                if len(page_text) > len(detail_text):
                    detail_text += f"\n\n{page_text}"

        new_jobs.append({"title": title, "url": job_url or job_id, "detail_text": detail_text})

    return {"total": len(positions), "new": new_jobs}


# ─── 6. TALEO RSS ───
def check_taleo(site, seen_urls):
    """Parse Taleo's RSS feed for job listings."""
    primary = site.get("primary_approach", {})
    rss_url = primary.get("url", "")

    if not rss_url:
        _record_error("Taleo: no RSS URL configured")
        print("    No RSS URL configured for Taleo site.")
        return None

    try:
        resp = request_with_retry("GET", rss_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
        _record_error(f"Taleo RSS: {e}")
        print(f"    Taleo RSS error: {e}")
        print("    (This site may need a headless browser — see notes in config)")
        return None

    items = root.findall(".//item")
    print(f"    RSS feed returned {len(items)} items")

    new_jobs = []
    for item in items:
        title = item.findtext("title", "Untitled")
        link = item.findtext("link", "")
        description = item.findtext("description", "")

        if link in seen_urls:
            continue

        detail_text = f"Title: {title}\n\n{description}"

        if link:
            time.sleep(1)
            detail_soup = fetch_page(link, proxy=site.get("proxy"))
            if detail_soup:
                page_text = extract_text(detail_soup)
                if len(page_text) > len(detail_text):
                    detail_text = f"Title: {title}\n\n{page_text}"

        new_jobs.append({"title": title, "url": link, "detail_text": detail_text})

    return {"total": len(items), "new": new_jobs}

# ─── 7. PALLADIUM AJAX API ───
def check_palladium(site, seen_urls):
    """Query Palladium's internal AJAX endpoint and filter by country."""
    api = site["api"]
    fields = api["job_fields"]
    country_filter = api.get("country_filter", "")
    url_template = api.get("job_url_template", "")

    hdrs = {**HEADERS, **api.get("headers", {})}

    try:
        resp = request_with_retry("GET", api["url"], headers=hdrs, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Palladium API: {e}")
        print(f"    Palladium API error: {e}")
        return None

    all_jobs = data.get(api["response_key"], [])

    if country_filter:
        filtered = [j for j in all_jobs if j.get(fields["country"], "") == country_filter]
        print(f"    API returned {len(all_jobs)} jobs, {len(filtered)} in '{country_filter}'")
    else:
        filtered = all_jobs
        print(f"    API returned {len(all_jobs)} jobs")

    new_jobs = []
    for job in filtered:
        job_id = str(job.get(fields["id"], ""))
        title = job.get(fields["title"], "Untitled")
        country = job.get(fields["country"], "")

        job_url = url_template.replace("{job_id}", job_id) if url_template and job_id else ""

        if job_url in seen_urls or job_id in seen_urls:
            continue

        detail_text = f"Title: {title}\nLocation: United Kingdom\nOrganisation: Palladium Group"

        new_jobs.append({
            "title": title,
            "url": job_url or job_id,
            "detail_text": detail_text,
            "_also_track": [u for u in [job_url, job_id] if u]
        })

    return {"total": len(filtered), "new": new_jobs}

# ─── 8. PINPOINT API (with cloudscraper for Cloudflare bypass) ───
def check_pinpoint(site, seen_urls):
    """Query a Pinpoint ATS /postings.json endpoint using cloudscraper."""
    api = site["api"]
    fields = api["job_fields"]
    base_url = site.get("base_url", "")

    scraper = cloudscraper.create_scraper()

    try:
        resp = scraper.get(api["url"], timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Pinpoint API: {e}")
        print(f"    Pinpoint API error: {e}")
        return None

    postings = data.get(api.get("response_key", "data"), [])
    if not isinstance(postings, list):
        postings = []
    print(f"    API returned {len(postings)} jobs")

    location_filter = site.get("location_filter", "")
    total_after_filter = 0
    new_jobs = []
    for posting in postings:
        title = posting.get(fields.get("title", "title"), "Untitled")
        job_url = posting.get(fields.get("url", "url"), "")
        location = posting.get(fields.get("location", "locationName"), "")
        if isinstance(location, dict):
            location = location.get("name", "") or location.get("label", "") or str(location)
        department = posting.get(fields.get("department", "departmentName"), "")

        # Location filter
        if location_filter and location_filter.lower() not in location.lower():
            continue

        total_after_filter += 1

        # Make URL absolute if needed
        if job_url and not job_url.startswith("http"):
            job_url = base_url.rstrip("/") + "/" + job_url.lstrip("/")

        if job_url in seen_urls:
            continue

        detail_text = f"Title: {title}\nLocation: {location}\nDepartment: {department}"

        # Fetch detail page
        if job_url:
            time.sleep(1)
            try:
                detail_resp = scraper.get(job_url, timeout=30)
                if detail_resp.status_code == 200:
                    detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
                    page_text = extract_text(detail_soup)
                    if len(page_text) > len(detail_text):
                        detail_text = f"Title: {title}\nLocation: {location}\n\n{page_text}"
            except Exception as e:
                print(f"    Could not fetch detail for {title}: {e}")

        new_jobs.append({"title": title, "url": job_url, "detail_text": detail_text})

    if location_filter:
        print(f"    After filtering: {total_after_filter} jobs")

    return {"total": total_after_filter if location_filter else len(postings), "new": new_jobs}

# ─── 9. PLAYWRIGHT (headless browser for JS-rendered sites) ───
def fetch_detail_playwright(url, wait_selector="", wait_ms=5000):
    """Fetch a detail page using Playwright and return extracted text."""
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(url, timeout=30000, wait_until="domcontentloaded")
            if wait_selector:
                try:
                    page.wait_for_selector(wait_selector, timeout=15000)
                except:
                    page.wait_for_timeout(wait_ms)
            else:
                page.wait_for_timeout(wait_ms)
            time.sleep(1)
            html = page.content()
            browser.close()
        soup = BeautifulSoup(html, "html.parser")
        return extract_text(soup)
    except Exception as e:
        # Non-fatal: caller falls back to listing-derived text. We still record
        # so a systemic detail-fetch failure isn't completely invisible. (main()
        # consumes-and-discards this when the handler ultimately returns a valid
        # result, so it never masks a healthy run.)
        _record_error(f"fetch_detail_playwright: {e}")
        print(f"    Playwright detail fetch error: {e}")
        return ""


def strip_query_params(url):
    """Remove query parameters from a URL for cleaner deduplication."""
    return url.split("?")[0].rstrip("/")


def check_playwright(site, seen_urls):
    """Use headless Chromium to scrape JS-rendered job listing pages."""
    listing_url = site["url"]
    link_selector = site.get("link_selector", "")
    base_url = site.get("base_url", "")
    selector = site.get("selector", "")
    wait_selector = site.get("wait_selector", "")
    wait_ms = site.get("wait_ms", 5000)
    card_selector = site.get("card_selector", "")
    title_selector = site.get("title_selector", "")
    should_strip_params = site.get("strip_url_params", False)
    detail_via_pw = site.get("detail_via_playwright", False)
    detail_wait_sel = site.get("detail_wait_selector", "")

    use_stealth = site.get("stealth", False)
    print(f"    Launching headless Chromium{'  (stealth)' if use_stealth else ''}...")
    try:
        if use_stealth and _STEALTH_MODE == "context":
            pw_cm = Stealth().use_sync(sync_playwright())
        else:
            pw_cm = sync_playwright()
        with pw_cm as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            if use_stealth and _STEALTH_MODE == "page":
                _stealth_sync(page)
            wait_until = site.get("wait_until", "networkidle")
            page.goto(listing_url, timeout=30000, wait_until=wait_until)
            if wait_selector:
                wait_timeout = site.get("wait_timeout", 15000)
                page.wait_for_selector(wait_selector, timeout=wait_timeout)
            else:
                page.wait_for_timeout(wait_ms)
            html = page.content()
            # Debug: what did we actually get?
            if use_stealth:
                from bs4 import BeautifulSoup as _BS
                _dbg = _BS(html, "html.parser")
                _h = [h.get_text(strip=True)[:50] for h in _dbg.select("h1, h2, h3")[:5]]
                print(f"    [stealth debug] {len(html)} chars, headings: {_h}")
            browser.close()
    except Exception as e:
        _record_error(f"Playwright: {e}")
        print(f"    Playwright error: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Hash-check mode (no link_selector)
    if not link_selector:
        text = extract_text(soup, selector)
        if len(text) < 50:
            print(f"    ⚠️ Very little content ({len(text)} chars)")
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        titles = []
        target_el = soup.select_one(selector) if selector else soup
        if target_el:
            for h in target_el.select("h3, h4, h5"):
                h_text = h.get_text(strip=True)
                if h_text and len(h_text) < 100:
                    titles.append(h_text)
        return {"type": "hash_check", "text": text, "hash": text_hash, "titles": titles, "soup": soup}

    # ── Card-based extraction (for Oracle HCM and similar SPAs) ──
    if card_selector and title_selector:
        cards = soup.select(card_selector)
        print(f"    Found {len(cards)} job card(s) via card_selector")

        all_urls = set()
        jobs = []
        seen_in_batch = set()

        for card in cards:
            # Extract title from dedicated selector
            t_el = card.select_one(title_selector)
            title = t_el.get_text(strip=True) if t_el else "Untitled"

            # Extract link
            a = card.select_one(link_selector)
            if not a:
                continue
            href = a.get("href", "").strip()
            if not href or href == "#":
                continue

            full_url = urljoin(base_url + "/", href) if base_url else urljoin(listing_url, href)

            # Optionally strip query params for dedup
            dedup_url = strip_query_params(full_url) if should_strip_params else full_url.rstrip("/")

            all_urls.add(dedup_url)
            if dedup_url not in seen_urls and dedup_url not in seen_in_batch:
                jobs.append({"title": title, "url": dedup_url, "full_url": full_url})
                seen_in_batch.add(dedup_url)

        new_jobs = []
        for job in jobs:
            time.sleep(1)
            if detail_via_pw:
                detail_text = fetch_detail_playwright(
                    job["full_url"], detail_wait_sel, wait_ms
                )
            else:
                detail_soup = fetch_page(job["full_url"], proxy=site.get("proxy"))
                detail_text = extract_text(detail_soup) if detail_soup else ""
            new_jobs.append({
                "title": job["title"],
                "url": job["url"],
                "detail_text": detail_text or f"Title: {job['title']} (detail page could not be loaded)"
            })

        return {"total": len(all_urls), "new": new_jobs}

    # ── Standard anchor-based extraction (existing logic) ──
    if site.get("scope_links") and selector:
        scope_el = soup.select_one(selector.split(",")[0].strip())
        anchors = scope_el.select(link_selector) if scope_el else []
    else:
        anchors = soup.select(link_selector)
    print(f"    Found {len(anchors)} job links after JS rendering")

    all_urls = set()
    jobs = []
    seen_in_batch = set()
    for a in anchors:
        href = a.get("href", "").strip()
        if not href or href == "#":
            continue

        title = a.get_text(strip=True) or "Untitled"
        # If the link wraps a card, prefer the heading inside it
        inner_heading = a.select_one("span[class*='h4'], h2, h3, h4, h5")
        if inner_heading:
            title = inner_heading.get_text(strip=True)
        if title.lower() in ("view job", "apply", "apply now", "learn more",
                              "read more", "click here", "view"):
            card = None
            node = a.parent
            card_keywords = ("item", "card", "career", "listing", "posting", "vacancy")
            while node and node.name:
                classes = " ".join(node.get("class", []))
                role = node.get("role", "")
                if any(kw in classes.lower() for kw in card_keywords) or role == "listitem":
                    card = node
                    break
                if node.name in ("article", "li"):
                    card = node
                    break
                node = node.parent

            if card:
                heading = card.select_one(
                    "[class*='subheadline'], [class*='headline'], [class*='heading'], h2, h3, h4, h5"
                )
                if heading:
                    title = heading.get_text(strip=True)
                else:
                    continue
            else:
                continue

        full_url = urljoin(base_url + "/", href) if base_url else urljoin(listing_url, href)
        full_url = full_url.rstrip("/")

        all_urls.add(full_url)
        if full_url not in seen_urls and full_url not in seen_in_batch:
            jobs.append({"title": title, "url": full_url})
            seen_in_batch.add(full_url)

    new_jobs = []
    for job in jobs:
        time.sleep(1)
        detail_soup = fetch_page(job["url"], proxy=site.get("proxy"))
        detail_text = extract_text(detail_soup) if detail_soup else ""
        new_jobs.append({
            "title": job["title"],
            "url": job["url"],
            "detail_text": detail_text or f"Title: {job['title']} (detail page could not be loaded)"
        })

    return {"total": len(all_urls), "new": new_jobs}

# ─── 10. ORACLE HCM REST API ───
def check_oracle_hcm(site, seen_urls):
    """Query Oracle HCM Cloud's public REST API for job listings."""
    api = site["api"]
    listing_url = api["listing_url"]
    detail_template = api.get("detail_url_template", "")
    page_template = api.get("job_page_template", "")

    try:
        resp = request_with_retry("GET", listing_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Oracle HCM API: {e}")
        print(f"    Oracle HCM API error: {e}")
        return None

    items = data.get("items", [])
    if not items:
        print(f"    No items in API response")
        return {"total": 0, "new": []}

    search_result = items[0]
    jobs = search_result.get("requisitionList", [])
    total = search_result.get("TotalJobsCount", len(jobs))
    print(f"    API returned {len(jobs)} job(s) (total: {total})")

    new_jobs = []
    for job in jobs:
        job_id = str(job.get("Id", ""))
        title = job.get("Title", "Untitled")
        posted = job.get("PostedDate", "")
        country = job.get("PrimaryLocationCountry", "")
        primary_loc = job.get("PrimaryLocation", "")
        category = job.get("Category", "")
        short_desc = job.get("ShortDescriptionStr", "")

        # Build the human-facing job page URL
        job_url = page_template.replace("{job_id}", job_id) if page_template else ""

        if job_url in seen_urls or job_id in seen_urls:
            continue

        # Build basic detail text from listing data
        detail_text = f"Title: {title}\nLocation: {primary_loc or country}\nCategory: {category}\nPosted: {posted}"

        # Fetch full description from detail API
        if detail_template and job_id:
            detail_api_url = detail_template.replace("{job_id}", job_id)
            try:
                time.sleep(1)
                dr = requests.get(detail_api_url, headers=HEADERS, timeout=30)
                dr.raise_for_status()
                dd = dr.json()
                detail_items = dd.get("items", [])
                if detail_items:
                    d = detail_items[0]
                    # Combine all description fields
                    desc_parts = []
                    for key in ["ExternalDescriptionStr", "ExternalQualificationsStr",
                                "ExternalResponsibilitiesStr"]:
                        html_val = d.get(key, "")
                        if html_val:
                            clean = BeautifulSoup(html_val, "html.parser").get_text(separator="\n", strip=True)
                            if clean:
                                desc_parts.append(clean)
                    if desc_parts:
                        detail_text = f"Title: {title}\nLocation: {primary_loc or country}\nCategory: {category}\nPosted: {posted}\n\n" + "\n\n".join(desc_parts)
            except Exception as e:
                print(f"    Could not fetch detail for {title}: {e}")

        new_jobs.append({
            "title": title,
            "url": job_url or job_id,
            "detail_text": detail_text,
            "_also_track": [u for u in [job_url, job_id] if u]
        })

    return {"total": total, "new": new_jobs}

# ─── 11. HIRESERVE ATS JSON API ───
def check_hireserve(site, seen_urls):
    """Query a Hireserve ATS JSON feed, with optional category filtering."""
    api = site["api"]
    fields = api["job_fields"]

    # Build URL with params + category filters
    params = api.get("params", {})
    query_string = "&".join(f"{k}={v}" for k, v in params.items())
    cat_filters = api.get("category_filters", [])
    if cat_filters:
        query_string += "&" + "&".join(
            f"p_category_code_arr={code}" for code in cat_filters
        )
    full_url = f"{api['url']}?{query_string}"

    try:
        resp = request_with_retry("GET", full_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Hireserve API: {e}")
        print(f"    Hireserve API error: {e}")
        return None

    postings = data.get(api["response_key"], [])
    print(f"    API returned {len(postings)} job(s)")

    new_jobs = []
    for posting in postings:
        job_id = str(posting.get(fields["id"], ""))
        title = posting.get(fields["title"], "Untitled")
        job_url = posting.get(fields["url"], "")

        # Extract business unit from classifications for detail text
        business_unit = ""
        classifications = posting.get("classifications", {})
        bu_class = classifications.get("class_17744", {})
        bu_values = bu_class.get("values", [])
        if bu_values:
            business_unit = bu_values[0].get("class_val", "")

        # Extract closing date
        closing = ""
        pub = posting.get("publication", {}).get("internet", {})
        closing = pub.get("closing_date", "")

        if job_url in seen_urls or job_id in seen_urls:
            continue

        detail_text = f"Title: {title}\nBusiness Unit: {business_unit}\nClosing: {closing}"

        # Fetch detail page
        if job_url:
            time.sleep(1)
            detail_soup = fetch_page(job_url, proxy=site.get("proxy"))
            if detail_soup:
                page_text = extract_text(detail_soup)
                if len(page_text) > len(detail_text):
                    detail_text = f"Title: {title}\nBusiness Unit: {business_unit}\nClosing: {closing}\n\n{page_text}"

        new_jobs.append({
            "title": title,
            "url": job_url or job_id,
            "detail_text": detail_text,
            "_also_track": [u for u in [job_url, job_id] if u]
        })

    return {"total": len(postings), "new": new_jobs}


# ─── 12. PLUMM CAREERS API (heyplumm.com) ───
def _plumm_slug(title):
    """Replicate Plumm's URL slug: lowercase, spaces→hyphens, strip punctuation
    (except hyphens and en-/em-dashes), URL-encode."""
    import re as _re
    import urllib.parse as _up
    s = title.lower().replace(" ", "-")
    # Keep word chars (letters/digits/underscore), hyphens, en-dash, em-dash
    s = _re.sub(r"[^\w\-\u2013\u2014]", "", s)
    return _up.quote(s, safe="-")


def check_plumm_api(site, seen_urls):
    """Query Plumm's CareerSite/GetOpenJobs endpoint."""
    api = site["api"]
    body = api.get("body", {})
    referer = api.get("referer", site.get("url", ""))
    location_filter = site.get("location_filter", "")
    job_url_template = site.get(
        "job_url_template",
        "https://app.heyplumm.com/jobs/{company}/{slug}/{id}?refer=Plumm",
    )

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": referer,
        "User-Agent": HEADERS["User-Agent"],
    }

    try:
        resp = request_with_retry("POST", api["url"], headers=headers, json=body, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Plumm API: {e}")
        print(f"    Plumm API error: {e}")
        return None

    if not data.get("status"):
        _record_error(f"Plumm API: status=false (raw: {str(data)[:200]})")
        print(f"    Plumm API returned status=false (raw: {str(data)[:200]})")
        return None

    postings = data.get("data", []) or []
    print(f"    API returned {len(postings)} job(s)")

    total_after_filter = 0
    new_jobs = []
    for posting in postings:
        title = posting.get("JobTitle", "Untitled")
        encoded_id = posting.get("EncodedId", "")
        company = (posting.get("companyNameUrl", "") or "").lower()
        city = posting.get("City", "") or ""
        country = posting.get("Country", "") or ""
        salary = posting.get("Salary", "") or ""
        job_type = posting.get("JobType", "") or ""
        workplace = posting.get("WorkplaceType", "") or ""
        time_elapsed = posting.get("TimeElapsed", "") or ""
        experience = posting.get("ExperienceRequired", "")

        # Location filter (case-insensitive substring on city + country)
        if location_filter:
            location_str = f"{city} {country}".lower()
            if location_filter.lower() not in location_str:
                continue

        total_after_filter += 1

        # Build job URL
        slug = _plumm_slug(title)
        job_url = (
            job_url_template
            .replace("{company}", company)
            .replace("{slug}", slug)
            .replace("{id}", encoded_id)
        )

        if job_url in seen_urls or encoded_id in seen_urls:
            continue

        detail_text = (
            f"Title: {title}\n"
            f"Location: {city}, {country}\n"
            f"Workplace Type: {workplace}\n"
            f"Job Type: {job_type}\n"
            f"Salary: {salary}\n"
            f"Experience Required: {experience} years\n"
            f"Posted: {time_elapsed}"
        )

        new_jobs.append({
            "title": title,
            "url": job_url,
            "detail_text": detail_text,
            "_also_track": [u for u in [job_url, encoded_id] if u],
        })

    if location_filter:
        print(f"    After filtering: {total_after_filter} job(s)")

    return {
        "total": total_after_filter if location_filter else len(postings),
        "new": new_jobs,
    }


# ─── 13. RIPPLING ATS API ───
def check_rippling_api(site, seen_urls):
    """Query Rippling's public board API.

    Endpoint shape: https://api.rippling.com/platform/api/ats/v1/board/{slug}/jobs
    Returns a JSON array of job objects with keys: uuid, name, url,
    workLocation: {label, id}, department: {label, id}.
    """
    api = site["api"]
    location_filter = site.get("location_filter", "")

    try:
        resp = request_with_retry(
            "GET",
            api["url"],
            headers={**HEADERS, "Accept": "application/json"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        _record_error(f"Rippling API: {e}")
        print(f"    Rippling API error: {e}")
        return None

    # Response is a list at the top level
    postings = data if isinstance(data, list) else data.get("jobs", []) or []
    print(f"    API returned {len(postings)} job(s)")

    total_after_filter = 0
    new_jobs = []
    for posting in postings:
        title = posting.get("name", "Untitled")
        job_url = posting.get("url", "")
        uuid = posting.get("uuid", "")
        loc = posting.get("workLocation") or {}
        location = loc.get("label", "") if isinstance(loc, dict) else str(loc)
        dept = posting.get("department") or {}
        department = dept.get("label", "") if isinstance(dept, dict) else str(dept)

        # Location filter: case-insensitive substring match.
        # Note: Rippling returns one primary workLocation, but multi-city jobs
        # often list every city in the job's title or detail page rather than
        # this field. Sites that want multi-location coverage should leave
        # location_filter empty and let the LLM filter on detail text.
        if location_filter:
            if location_filter.lower() not in location.lower():
                continue

        total_after_filter += 1

        if job_url in seen_urls or uuid in seen_urls:
            continue

        detail_text = (
            f"Title: {title}\n"
            f"Location: {location}\n"
            f"Department: {department}"
        )

        # Fetch detail page for full description if not explicitly disabled
        if not site.get("skip_detail_fetch") and job_url:
            time.sleep(1)
            detail_soup = fetch_page(job_url, proxy=site.get("proxy"))
            if detail_soup:
                page_text = extract_text(detail_soup)
                if len(page_text) > len(detail_text):
                    detail_text = (
                        f"Title: {title}\n"
                        f"Location: {location}\n"
                        f"Department: {department}\n\n{page_text}"
                    )

        new_jobs.append({
            "title": title,
            "url": job_url or uuid,
            "detail_text": detail_text,
            "_also_track": [u for u in [job_url, uuid] if u],
        })

    if location_filter:
        print(f"    After filtering: {total_after_filter} job(s)")

    return {
        "total": total_after_filter if location_filter else len(postings),
        "new": new_jobs,
    }


# ═══════════════════════════════════════════════════════════════
#  DISPATCHER — routes each site to the correct handler
# ═══════════════════════════════════════════════════════════════

METHOD_HANDLERS = {
    "html": check_html,
    "workday_api": check_workday,
    "greenhouse_api": check_greenhouse,
    "workable_api": check_workable,
    "personio_xml": check_personio,
    "taleo_rss": check_taleo,
    "palladium_api": check_palladium,
    "pinpoint_api": check_pinpoint,
    "playwright": check_playwright,
    "oracle_hcm_api": check_oracle_hcm,
    "hireserve_api": check_hireserve,
    "plumm_api": check_plumm_api,
    "rippling_api": check_rippling_api,
}


# ═══════════════════════════════════════════════════════════════
#  AI MATCHING — send job details to Anthropic
# ═══════════════════════════════════════════════════════════════
#
# Architecture:
#   - System prompt holds the rubric, format, examples, and CV. It is
#     identical across every call so we mark it as cacheable. Anthropic
#     reads cached input at ~10% of normal price, which roughly cuts
#     monthly cost in half.
#   - User message holds only the per-call data: org name, URL, job text,
#     and a one-line marker switching between page-level and single-job
#     mode. This part is dynamic and not cached.
#   - Determinism on this scoring task comes from the rubric structure and
#     the formula-based MATCH derivation in the system prompt. Note that
#     Claude Opus 4.7 rejects temperature/top_p/top_k parameters outright
#     (400 error) — these must be omitted.

SYSTEM_PROMPT = f"""You are an expert career assistant evaluating job postings against the CV of one specific candidate. The candidate's full CV is included at the end of this prompt — refer to it whenever you assess a role.

────────────────────────────────────────
CANDIDATE TARGETING (READ FIRST)
────────────────────────────────────────

The candidate is an MSc IR researcher targeting roles across three broad
zones, ALL of which are valid application targets:

  (a) Academic / policy research — think-tanks, universities, multilateral
      bodies, NGOs working on international law, atrocity prevention,
      transitional justice, European foreign policy, security studies,
      higher education policy.
  (b) Commercial intelligence & geopolitical risk — consultancies and
      analyst roles at firms like Control Risks, Sibylline, S-RM, IISS,
      Hakluyt, Verisk Maplecroft, Eurasia Group, Teneo political risk,
      and similar geopolitical/threat-intelligence operations.
  (c) Hybrid research-engineer / OSINT / digital investigations —
      computational research at policy institutes (CETaS at Alan Turing,
      Ada Lovelace, Oxford Internet Institute, AI Security Institute),
      open-source investigations (Bellingcat, Centre for Information
      Resilience, Forensic Architecture), digital humanities labs,
      tech/AI/platform policy research.

Do NOT penalise zone (b) or zone (c) roles relative to zone (a). The
candidate's CV may emphasise academic interests — read past surface framing
and recognise that commercial intelligence and hybrid technical roles are
core targets, not stretches. Theoretical interests on the CV are background
context; the candidate's actual applications span all three zones.

────────────────────────────────────────
ASSESSMENT FRAMEWORK
────────────────────────────────────────

You will rate each job on three dimensions, scored 1–5.

FIELD alignment
  5 — Core fit. Any of:
       • International law, transitional justice, genocide prevention,
         human rights research, refugee/displacement research
       • IR research/policy, European foreign policy, security studies,
         conflict analysis, defence policy
       • Think-tank or university research roles in the above areas
       • Higher education policy, research administration, research funding
       • Commercial intelligence, geopolitical risk advisory, threat
         intelligence analyst roles at consultancies (Sibylline, Control
         Risks, S-RM, IISS, Verisk Maplecroft, Eurasia Group, Hakluyt,
         Teneo political risk, etc.)
       • OSINT, open-source investigations, digital verification,
         atrocity documentation using computational methods
       • Research-engineer / computational researcher roles at policy
         institutes (CETaS, Ada Lovelace, OII, AISI, similar)
       • Tech policy, AI policy, AI governance, platform governance,
         surveillance and digital rights research
  4 — Strong fit. Policy roles in major NGOs / multilateral bodies that
       sit adjacent to core areas; diplomatic / parliamentary research;
       digital humanities and computational social science research roles;
       roles at advocacy organisations on related issues.
  3 — Adjacent: government affairs, public affairs / political consulting,
       broader sociology / history / political science research, comms roles
       attached to a relevant policy area, generalist consulting at firms
       with a public-sector or policy practice.
  2 — Weak: general comms/marketing/PR for non-policy clients, generic
       project management, general operations in unrelated sectors,
       corporate comms not tied to policy or risk.
  1 — Mismatch: pure sales, HR/talent, finance/accounting, software
       engineering for unrelated products, healthcare/clinical, design/
       creative, building services, lab work.

SKILLS match
  The candidate has: research methods, qualitative & quantitative analysis
  (SPSS, Python, Excel), policy/academic writing, literature review, project
  administration; languages German (native), English (C2), French (B1).
  5 — All required skills demonstrably present in the CV.
  4 — Most required skills present; minor gaps the candidate could close
       quickly with their transferable analytical/writing background.
  3 — About half present; the rest are reasonable transferable skills.
  2 — Specific tooling or methods the candidate lacks (e.g., specific GIS
       software, advanced econometrics, specific CRM platforms).
  1 — Specialist technical skills the candidate cannot substitute for
       (clinical practice, accountancy software, civil engineering tools,
       qualified-lawyer drafting, hands-on lab work).

SENIORITY fit
  5 — Internships, working-student roles, research / admin / programme
       assistant, junior analyst, "graduate scheme", entry-level positions
       explicitly open to those with no full-time professional experience.
  4 — Roles asking for ~1–2 years of experience or equivalent. The
       candidate's substantial student-assistant experience often counts.
  3 — Roles asking for ~3 years of post-degree experience.
  2 — Roles asking for 4 years, or "Manager" titles where management
       experience is preferred but not strictly required.
  1 — Senior, Lead, Director, Head, Partner, Principal titles; roles
       requiring 5+ years as a hard requirement; explicit PhD requirement;
       specialist licences/registrations the candidate doesn't hold.

────────────────────────────────────────
HARD INELIGIBILITY RULES
────────────────────────────────────────

If ANY of the following apply, set SENIORITY = 1 and MATCH = Low regardless
of how strong the field or skills alignment is:

  • Requires a PhD that is awarded, near-completion, or "by the start date"
  • Requires a UK qualifying law degree, GDL, SQE, or CILEX (the candidate
     does not have one — applies to JUSTICE roles, paralegal positions, etc.)
  • Requires NMC nursing/midwifery registration, GMC medical registration,
     dental registration, or other clinical practice licence
  • Requires ACA, ACCA, CIMA, AAT or equivalent accounting qualification
  • Requires fluency in a language the candidate does not speak — the
     candidate has German, English, and basic French only. Roles requiring
     Spanish, Portuguese, Russian, Mandarin, Arabic, Czech, Polish, etc.
     for the actual work (not "nice to have") trigger this rule.
  • Requires 5+ years of professional full-time experience as a HARD
     requirement. Note carefully: "ideally 5 years", "preferably",
     "5+ desirable" are NOT hard requirements — they are preferences. Only
     exclude when the requirement is explicit and non-negotiable.
  • Requires a specific vocational training (e.g., German "Ausbildung" in a
     trade like Mechatronik, Sanitär-/Klimatechnik) that the candidate lacks

────────────────────────────────────────
OVERALL MATCH RATING (deterministic mapping)
────────────────────────────────────────

After scoring the three dimensions, derive MATCH using this exact rule:

  HIGH    — All three scores ≥ 4 AND no hard ineligibility triggered.
  MEDIUM  — At least one score ≥ 4, no score = 1, no hard ineligibility.
  LOW     — Any score = 1, OR two or more scores = 2, OR any hard
            ineligibility rule triggered.

Do NOT factor location into the rating. Report it for information only —
a role in another city or country does not lower the rating.

Err on the side of inclusion for genuinely relevant roles where seniority
fits: when in doubt between Medium and Low for a plausibly relevant role,
choose Medium.

────────────────────────────────────────
OUTPUT FORMAT
────────────────────────────────────────

Use these exact labels, one per line, in this order. The user message will
provide the ORGANISATION and URL — copy them verbatim into your response.

JOB: [job title]
ORGANISATION: [as provided in the user message]
LOCATION: [city/country from job content; "Not specified" if absent]
TYPE: [Full-time / Part-time / Internship / Contract; "Not specified" if absent]
DEADLINE: [application deadline if stated; "Not specified" if absent]
SALARY: [salary or pay range if stated; "Not specified" if absent]
MATCH: [High / Medium / Low]
FIELD: [1-5]
SKILLS: [1-5]
SENIORITY: [1-5]
REASON: [2-3 sentences. Lead with the strongest signal — alignment or gap. If a hard ineligibility rule applies, name it explicitly.]
URL: [as provided in the user message]

PAGE-LEVEL MODE
If the user message begins with "PAGE-LEVEL SCAN", the content is a careers
page that may list multiple jobs. Identify each individual job posting and
emit the format above for each one, separated by a blank line. If no jobs
are found on the page at all, respond with exactly: NO_JOBS_FOUND

LANGUAGE
Job content may be in German, French, Spanish, etc. Assess regardless of
the source language. Output in English.

────────────────────────────────────────
CALIBRATION EXAMPLES
────────────────────────────────────────

Example 1 — postdoctoral role (hard ineligibility)
A "Postdoctoral Research Associate" role in critical IR at Queen Mary that
explicitly requires a completed PhD →
  FIELD: 5  SKILLS: 4  SENIORITY: 1  MATCH: Low
  REASON: Field alignment is excellent (critical IR is core to candidate's
  interests) but the role explicitly requires a completed PhD, which the
  candidate (currently MSc) does not hold. Hard ineligibility on seniority.

Example 2 — adjacent field, junior level (Medium)
An "Account Executive — Healthcare Policy & Public Affairs" role at Hanover,
described as a junior recruit role with intro-to-public-affairs framing →
  FIELD: 3  SKILLS: 4  SENIORITY: 5  MATCH: Medium
  REASON: Healthcare policy is outside the candidate's stated core interests
  but is policy-adjacent. The junior framing matches their stage perfectly,
  and their research/policy-writing background transfers cleanly.

Example 3 — direct fit (High)
A "Research Assistant for Defence and Military Analysis Programme" at IISS,
asking for a strong background in IR/security and research/writing skills →
  FIELD: 5  SKILLS: 5  SENIORITY: 5  MATCH: High
  REASON: Direct overlap with candidate's IR/security focus and forthcoming
  defence-policy publication. RA level is exactly appropriate for their
  MSc-in-progress and substantial research-assistant experience.

Example 4 — senior leadership (hard ineligibility on years)
A "Director of Convening" at Chatham House, demanding extensive senior
leadership experience and high-level event management →
  FIELD: 5  SKILLS: 2  SENIORITY: 1  MATCH: Low
  REASON: Field alignment is excellent but the Director title requires
  significant leadership experience the candidate (only student-assistant
  roles to date) does not have. Hard ineligibility on seniority.

Example 5 — commercial intelligence (High, despite commercial context)
An "Associate Threat Intelligence Analyst" role at Sibylline, doing
geopolitical risk analysis and producing client-facing intelligence
reports, asking for IR/security background and strong research/writing →
  FIELD: 5  SKILLS: 4  SENIORITY: 5  MATCH: High
  REASON: Commercial intelligence and geopolitical risk are core targets
  for this candidate — do not treat as a downgrade from academic policy
  work. IR/security background, research methods, and policy writing are
  directly applicable; the Associate level fits their experience cleanly.

Example 6 — hybrid research-engineer (High)
A "Research Assistant" role at CETaS (Centre for Emerging Technology
and Security, Alan Turing Institute) working on AI governance and
security policy, asking for policy research skills plus comfort with
quantitative methods or basic programming →
  FIELD: 5  SKILLS: 4  SENIORITY: 5  MATCH: High
  REASON: This is exactly the hybrid policy-plus-technical zone the
  candidate targets. IR/security policy training matches the substantive
  focus; Python and data analysis skills meet the technical side; RA
  level matches the candidate's stage.

────────────────────────────────────────
CANDIDATE CV
────────────────────────────────────────

{CANDIDATE_CV}
"""


def evaluate_with_anthropic(site_name, job_title, job_url, detail_text, is_page_level=False):
    anthropic_rate_limit()

    # Build the per-call user message. Everything that varies between calls
    # goes here; everything static lives in SYSTEM_PROMPT and is cached.
    if is_page_level:
        user_message = (
            f"PAGE-LEVEL SCAN\n"
            f"ORGANISATION: {site_name}\n"
            f"URL: {job_url}\n\n"
            f"PAGE CONTENT:\n{detail_text[:10000]}"
        )
    else:
        user_message = (
            f"SINGLE JOB EVALUATION\n"
            f"JOB: {job_title}\n"
            f"ORGANISATION: {site_name}\n"
            f"URL: {job_url}\n\n"
            f"JOB DESCRIPTION:\n{detail_text[:10000]}"
        )

    headers = {
        "x-api-key": ANTHROPIC_API_KEY,
        "anthropic-version": "2023-06-01",
        "content-type": "application/json",
    }
    payload = {
        "model": ANTHROPIC_MODEL,
        "max_tokens": 4096,
        # NOTE: Claude Opus 4.7 rejects temperature/top_p/top_k with a 400
        # error. Determinism on this scoring task comes from the rubric and
        # the formula-based MATCH derivation in the system prompt, not from
        # sampling parameters. Do not re-add temperature here.
        "system": [
            {
                "type": "text",
                "text": SYSTEM_PROMPT,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        "messages": [{"role": "user", "content": user_message}],
    }

    # Transient API failures (429 rate-limit, 529 overloaded, 5xx) are common
    # and self-resolve. Previously a single one produced a permanent
    # llm_call_failed for that job. Retry those a few times (honouring the
    # Retry-After header) while still failing fast on real errors like 400.
    ANTHROPIC_RETRY_CODES = {429, 500, 502, 503, 504, 529}
    ANTHROPIC_MAX_ATTEMPTS = 4
    last_err = ""
    for attempt in range(1, ANTHROPIC_MAX_ATTEMPTS + 1):
        try:
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers=headers, json=payload, timeout=60,
            )
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as e:
            last_err = f"Anthropic API: {e}"
            if attempt < ANTHROPIC_MAX_ATTEMPTS:
                wait = 5 * attempt
                print(f"    Anthropic network error — retry in {wait}s "
                      f"(attempt {attempt}/{ANTHROPIC_MAX_ATTEMPTS}): {e}")
                time.sleep(wait)
                continue
            break
        except Exception as e:
            _record_error(f"Anthropic API: {e}")
            print(f"    Anthropic error: {e}")
            return None

        # Retryable status: back off (prefer server's Retry-After) and try again
        if resp.status_code in ANTHROPIC_RETRY_CODES and attempt < ANTHROPIC_MAX_ATTEMPTS:
            ra = resp.headers.get("retry-after", "")
            try:
                wait = float(ra)
            except (TypeError, ValueError):
                wait = 5 * attempt
            wait = min(wait, 30)
            last_err = f"Anthropic API: HTTP {resp.status_code}"
            print(f"    Anthropic HTTP {resp.status_code} — retry in {wait:.0f}s "
                  f"(attempt {attempt}/{ANTHROPIC_MAX_ATTEMPTS})")
            time.sleep(wait)
            continue

        # Surface the API's own error message on non-2xx responses, not just
        # the generic HTTP status. Anthropic returns a JSON body with an
        # explanatory message that is essential for debugging.
        if resp.status_code >= 400:
            body_snippet = (resp.text or "")[:1000]
            err_msg = f"HTTP {resp.status_code}: {body_snippet}"
            _record_error(f"Anthropic API: {err_msg}")
            print(f"    Anthropic error: {err_msg}")
            return None

        try:
            data = resp.json()
        except Exception as e:
            _record_error(f"Anthropic API: invalid JSON response: {e}")
            print(f"    Anthropic error: invalid JSON response: {e}")
            return None
        result = "".join(
            b.get("text", "") for b in data.get("content", [])
            if b.get("type") == "text"
        )
        # Log token usage and cache stats — helps confirm caching is working
        usage = data.get("usage", {})
        cache_read = usage.get("cache_read_input_tokens", 0)
        cache_write = usage.get("cache_creation_input_tokens", 0)
        in_tok = usage.get("input_tokens", 0)
        out_tok = usage.get("output_tokens", 0)
        print(
            f"    [tokens] in={in_tok} out={out_tok} "
            f"cache_read={cache_read} cache_write={cache_write}"
        )
        print(f"    --- Anthropic raw response ---")
        print(result)
        print(f"    --- End Anthropic response ---")
        return result

    # Exhausted retries on a transient error.
    _record_error(last_err or "Anthropic API: exhausted retries on transient error")
    print(f"    Anthropic error: exhausted retries ({last_err})")
    return None


# ═══════════════════════════════════════════════════════════════
#  TELEGRAM NOTIFICATIONS
# ═══════════════════════════════════════════════════════════════

def escape_html(text):
    """Escape characters that break Telegram's HTML parser."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

def parse_gemini_matches(raw_text):
    """Parse Gemini's structured output into a list of match dicts."""
    matches = []
    if not raw_text or "NO_JOBS_FOUND" in raw_text:
        return matches

    FIELDS = ["JOB", "ORGANISATION", "LOCATION", "TYPE", "DEADLINE", "SALARY", "MATCH", "FIELD", "SKILLS", "SENIORITY", "REASON", "URL"]

    def is_field_line(line):
        """Check if a line starts with a known field label. Returns (field_name, value) or None."""
        # Strip markdown bold/italic markers and whitespace
        cleaned = line.strip().lstrip("*#- ").rstrip("*")
        upper = cleaned.upper()
        for field in FIELDS:
            if upper.startswith(field + ":"):
                value = cleaned[len(field) + 1:].strip().lstrip("*").rstrip("*").strip()
                return field.lower(), value
        return None

    # Split on "JOB:" to get individual match blocks
    blocks = raw_text.split("JOB:")
    for block in blocks[1:]:  # skip everything before the first JOB:
        match = {}
        lines = block.strip().splitlines()
        # First line is the job title (remainder after "JOB:" split)
        match["job"] = lines[0].strip().strip("* ")

        current_field = None
        for line in lines[1:]:
            parsed = is_field_line(line)
            if parsed:
                current_field, value = parsed
                match[current_field] = value
            elif current_field == "reason":
                # Append continuation lines to the reason field
                continuation = line.strip()
                if continuation:
                    match["reason"] = match.get("reason", "") + " " + continuation

        if match.get("job"):
            matches.append(match)
    return matches

def _match_to_report_entry(m, fallback_org="", fallback_url=""):
    """Convert a parsed Gemini match dict into a daily report entry."""
    return {
        "title": m.get("job", "Untitled"),
        "organisation": m.get("organisation", fallback_org),
        "url": m.get("url", fallback_url),
        "match": m.get("match", "low").lower(),
        "field_score": m.get("field", ""),
        "skills_score": m.get("skills", ""),
        "seniority_score": m.get("seniority", ""),
        "reason": m.get("reason", ""),
        "location": m.get("location", ""),
        "type": m.get("type", ""),
        "deadline": m.get("deadline", ""),
        "salary": m.get("salary", ""),
    }

def format_match_for_telegram(match):
    """Format a single parsed match into a Telegram HTML message block."""
    title = escape_html(match.get("job", "Untitled"))
    org = escape_html(match.get("organisation", ""))
    location = match.get("location", "")
    job_type = match.get("type", "")
    deadline = match.get("deadline", "")
    salary = match.get("salary", "")
    field_score = match.get("field", "")
    skills_score = match.get("skills", "")
    seniority_score = match.get("seniority", "")
    level = match.get("match", "")
    reason = escape_html(match.get("reason", ""))
    url = match.get("url", "")

    # Match level emoji
    level_emoji = "🟢" if level.lower() == "high" else "🟡"

    parts = []
    parts.append(f"{level_emoji} <b>{title}</b>")
    if org:
        parts.append(f"🏢 {org}")

    # Info line: location, type, deadline — only include if specified
    info_bits = []
    if location and location.lower() != "not specified":
        info_bits.append(f"📍 {escape_html(location)}")
    if job_type and job_type.lower() != "not specified":
        info_bits.append(f"📋 {escape_html(job_type)}")
    if deadline and deadline.lower() != "not specified":
        info_bits.append(f"⏰ {escape_html(deadline)}")
    if salary and salary.lower() != "not specified":
        info_bits.append(f"💰 {escape_html(salary)}")
    if info_bits:
        parts.append(" · ".join(info_bits))

    # Score breakdown
    scores = []
    if field_score:
        scores.append(f"Field: {field_score}/5")
    if skills_score:
        scores.append(f"Skills: {skills_score}/5")
    if seniority_score:
        scores.append(f"Seniority: {seniority_score}/5")
    if scores:
        parts.append(f"📊 {' · '.join(scores)}")

    if reason:
        parts.append(f"\n{reason}")

    if url:
        parts.append(f'\n🔗 <a href="{url}">View posting</a>')

    return "\n".join(parts)

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    chunks = [message[i:i + 4000] for i in range(0, len(message), 4000)]
    for chunk in chunks:
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }
        try:
            resp = requests.post(url, json=payload, timeout=15)
            if resp.status_code == 400:
                payload["parse_mode"] = ""
                requests.post(url, json=payload, timeout=15)
        except Exception as e:
            print(f"    Telegram error: {e}")


# ═══════════════════════════════════════════════════════════════
#  MAIN PIPELINE
# ═══════════════════════════════════════════════════════════════

def main():
    now = datetime.now(timezone.utc)
    print(f"=== Job Monitor Run: {now.isoformat()} ===")
    if DRY_RUN:
        print("🏃 DRY RUN — populating state only, no LLM calls or notifications\n")
    print()

    state = load_state()
    issues_data = issues.load()
    all_matches = []
    daily_report_jobs = []
    paused_sites = []
    empty_sites = []

    for site in SITES:
        name = site["name"]
        method = site["method"]
        site_key = site["url"]

        print(f"\n[{site.get('id', '?')}] {name} ({method})")

        if site_key not in state:
            state[site_key] = {"seen_urls": [], "last_checked": "", "listing_hash": ""}

        # Check if site is paused due to repeated failures
        paused_until = state[site_key].get("paused_until", "")
        if paused_until:
            pause_end = datetime.fromisoformat(paused_until)
            if now < pause_end:
                remaining = (pause_end - now).total_seconds() / 3600
                print(f"    ⏸️ Paused until {paused_until[:16]} ({remaining:.0f}h remaining) — skipping.")
                continue
            else:
                print(f"    🔄 Pause expired — retrying...")
                state[site_key]["consecutive_errors"] = 0
                state[site_key].pop("paused_until", None)

        # Load seen URLs as an ordered list (for state persistence) and a
        # parallel set (handlers use O(1) `in` checks). Order is preserved
        # so that the prune-to-last-200 step at the end correctly drops the
        # OLDEST entries rather than an arbitrary subset.
        seen_urls_ordered = list(state[site_key].get("seen_urls", []))
        seen_urls = set(seen_urls_ordered)
        handler = METHOD_HANDLERS.get(method)

        if not handler:
            print(f"    Unknown method: {method}. Skipping.")
            issues.add(
                issues_data, now, site,
                "unknown_method",
                f"No handler registered for method '{method}'",
            )
            continue

        result = handler(site, seen_urls)
        last_err = _consume_error()

        if result is None:
            prev_errors = state[site_key].get("consecutive_errors", 0)
            state[site_key]["consecutive_errors"] = prev_errors + 1
            state[site_key]["last_checked"] = now.isoformat()
            err_count = state[site_key]["consecutive_errors"]

            issues.add(
                issues_data, now, site,
                "fetch_error",
                last_err or "Handler returned None (no error captured)",
                consecutive_count=err_count,
            )

            if err_count >= 16:
                pause_end = now + timedelta(days=2)
                state[site_key]["paused_until"] = pause_end.isoformat()
                paused_sites.append(name)
                print(f"    ⏸️ Failed {err_count} times — pausing until {pause_end.isoformat()[:16]}.")
                issues.add(
                    issues_data, now, site,
                    "site_paused",
                    f"Site paused after {err_count} consecutive failures",
                    consecutive_count=err_count,
                    paused_until=pause_end.isoformat(),
                )
            else:
                print(f"    ⚠️ Failed ({err_count}/16 before pause).")
            continue

        state[site_key]["consecutive_errors"] = 0

        # Handle the hash-check case (HTML sites without link_selector)
        if isinstance(result, dict) and result.get("type") == "hash_check":
            # Check for "no vacancies" pages
            page_lower = result["text"].lower()
            if not site.get("skip_no_vacancy_check", False) and any(phrase in page_lower for phrase in NO_VACANCY_PHRASES):
                print(f"    ℹ️ No vacancies listed (page says so).")
                empty_sites.append(name)
                state[site_key]["listing_hash"] = result["hash"]
                state[site_key]["last_checked"] = now.isoformat()
                continue

            # Check for boilerplate-only pages (e.g. REDRESS)
            boilerplate = site.get("no_vacancy_boilerplate", "")
            if boilerplate:
                stripped = page_lower.replace(boilerplate.lower(), "").strip()
                for heading in ["current vacancies", "vacancies", "careers", "jobs"]:
                    stripped = stripped.replace(heading, "").strip()
                if len(stripped) < 50:
                    print(f"    ℹ️ No vacancies listed (only boilerplate text found).")
                    empty_sites.append(name)
                    state[site_key]["listing_hash"] = result["hash"]
                    state[site_key]["last_checked"] = now.isoformat()
                    continue

            title_selector = site.get("job_title_selector", "")
            if title_selector and result.get("soup"):
                title_elements = result["soup"].select(title_selector)
                if title_elements:
                    titles = [el.get_text(strip=True) for el in title_elements if el.get_text(strip=True)]
                    for t in titles:
                        print(f"      → {t}")
                else:
                    print(f"    (no titles matched selector '{title_selector}')")
                    
            old_hash = state[site_key].get("listing_hash", "")
            if result["hash"] != old_hash:
                for t in result.get("titles", []):
                    print(f"      → {t}")
                if DRY_RUN:
                    print(f"    Page content changed (dry run — skipping LLM)")
                    # In dry-run we deliberately advance the hash to seed state.
                    state[site_key]["listing_hash"] = result["hash"]
                else:
                    print(f"    Page content changed! Sending full text to Anthropic...")
                    llm_result = evaluate_with_anthropic(name, f"Page update on {name}", site["url"], result["text"], is_page_level=True)
                    llm_err = _consume_error()
                    if llm_result:
                        parsed = parse_gemini_matches(llm_result)
                        for m in parsed:
                            # Telegram: High/Medium only
                            if m.get("match", "").lower() in ("high", "medium"):
                                all_matches.append(format_match_for_telegram(m))
                            # Daily report: all jobs
                            daily_report_jobs.append(_match_to_report_entry(m, fallback_org=name, fallback_url=site["url"]))
                        # Only commit the new hash after a successful LLM call.
                        # If the call failed we leave the OLD hash in place so
                        # the next run will retry the page-level evaluation.
                        state[site_key]["listing_hash"] = result["hash"]
                    else:
                        print(f"    ⚠️ LLM call failed — hash NOT updated, will retry next run.")
                        issues.add(
                            issues_data, now, site,
                            "llm_call_failed",
                            llm_err or "LLM returned no result for page-level call",
                            scope="page_level",
                        )
            else:
                print(f"    No changes detected.")
            state[site_key]["last_checked"] = now.isoformat()
            continue

        # Normal case: unpack total/new dict
        total_found = result["total"]
        new_jobs = result["new"]

        if total_found == 0:
            print(f"    ℹ️ No vacancies listed.")
            empty_sites.append(name)
            state[site_key]["last_checked"] = now.isoformat()
            continue

        if not new_jobs:
            print(f"    No new jobs (of {total_found} listed).")
            state[site_key]["last_checked"] = now.isoformat()
            continue

        print(f"    {len(new_jobs)} new job(s) of {total_found} listed! {'Saving to state (dry run)' if DRY_RUN else 'Evaluating...'}")

        for job in new_jobs:
            print(f"      → {job['title']}")

            if not DRY_RUN:
                llm_result = evaluate_with_anthropic(name, job["title"], job["url"], job["detail_text"])
                llm_err = _consume_error()

                if llm_result is None:
                    # LLM call failed — do NOT mark as seen, retry next run
                    print(f"        ⚠️ Anthropic call failed — will retry next run.")
                    issues.add(
                        issues_data, now, site,
                        "llm_call_failed",
                        llm_err or "LLM returned no result",
                        job_title=job["title"],
                        job_url=job["url"],
                    )
                    continue

                parsed = parse_gemini_matches(llm_result)
                if parsed:
                    m = parsed[0]
                    match_level = m.get("match", "low").lower()

                    # Telegram: High/Medium only
                    if match_level in ("high", "medium"):
                        all_matches.append(format_match_for_telegram(m))
                        print(f"        ✅ Match ({match_level})!")
                    else:
                        print(f"        No match (low).")

                    # Daily report: all jobs
                    daily_report_jobs.append(_match_to_report_entry(m, fallback_org=name, fallback_url=job["url"]))
                else:
                    # LLM returned something unparseable — record minimal entry
                    print(f"        ⚠️ Could not parse LLM response.")
                    issues.add(
                        issues_data, now, site,
                        "llm_parse_failed",
                        "LLM response could not be parsed into a match block",
                        job_title=job["title"],
                        job_url=job["url"],
                        raw_snippet=(llm_result or "")[:300],
                    )
                    daily_report_jobs.append({
                        "title": job["title"],
                        "organisation": name,
                        "url": job["url"],
                        "match": "low",
                        "location": "", "type": "", "deadline": "", "salary": "",
                        "field_score": "", "skills_score": "", "seniority_score": "",
                        "reason": "",
                    })

            # Track this URL (and any aliases) in BOTH the set (O(1) lookup
            # for the rest of this run) and the ordered list (preserves
            # insertion order for state persistence + prune-by-recency).
            if job["url"] not in seen_urls:
                seen_urls.add(job["url"])
                seen_urls_ordered.append(job["url"])
            for extra in job.get("_also_track", []):
                if extra not in seen_urls:
                    seen_urls.add(extra)
                    seen_urls_ordered.append(extra)
            time.sleep(1)

        state[site_key]["seen_urls"] = seen_urls_ordered
        state[site_key]["last_checked"] = now.isoformat()

    # ─── Prune seen_urls to prevent state.json bloat ───
    for site_key, site_state in state.items():
        seen = site_state.get("seen_urls", [])
        if len(seen) > 200:
            site_state["seen_urls"] = seen[-200:]
            print(f"  Pruned seen_urls for {site_key} to last 200 entries")

    # ─── Save state ───
    save_state(state)

    # ─── Save issues log ───
    issues.finalize(issues_data, state, now)

    # ─── Send results ───
    if DRY_RUN:
        print(f"\n🏃 Dry run complete. State populated for {len(SITES)} sites.")
        if empty_sites:
            print(f"   Sites with no vacancies: {', '.join(empty_sites)}")
        print(f"   Next run will only flag genuinely new jobs.")
    elif all_matches:
        header = f"🔍 <b>Job Monitor Report</b>\n<i>{now.strftime('%Y-%m-%d %H:%M')} UTC</i>\n<i>{len(all_matches)} match(es) found</i>"
        send_telegram(header)
        for match_msg in all_matches:
            send_telegram(match_msg)
            time.sleep(0.5)
        print(f"\n✅ Sent {len(all_matches)} match(es) to Telegram.")
    else:
        print("\nNo matching jobs found this run.")

    if paused_sites and not DRY_RUN:
        pause_msg = f"⏸️ <b>Sites Paused (16+ failures)</b>\n{escape_html(', '.join(paused_sites))}\nWill automatically retry in 2 days."
        send_telegram(pause_msg)

    if paused_sites:
        print(f"Paused: {', '.join(paused_sites)}")
    if empty_sites:
        print(f"Empty: {', '.join(empty_sites)}")

    # ─── Write daily report for the dashboard ───
    if not DRY_RUN:
        save_report(daily_report_jobs, now)


if __name__ == "__main__":
    main()

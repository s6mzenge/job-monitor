import json
import os
import hashlib
import requests
import time
import xml.etree.ElementTree as ET
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
import cloudscraper
from playwright.sync_api import sync_playwright

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
GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
DRY_RUN_FILE = "dry_run.txt"
if os.path.exists(DRY_RUN_FILE):
    with open(DRY_RUN_FILE, "r") as f:
        DRY_RUN = f.read().strip().lower() == "true"
else:
    DRY_RUN = False
CF_WORKER_URL = os.environ.get("CF_WORKER_URL", "")
CF_WORKER_TOKEN = os.environ.get("CF_WORKER_TOKEN", "")


# ─── Gemini rate limiter (15 RPM on free tier) ───
_gemini_calls = []

def gemini_rate_limit():
    """Enforce max 14 calls per 60 seconds (staying under 15 RPM limit)."""
    global _gemini_calls
    now_ts = time.time()
    _gemini_calls = [t for t in _gemini_calls if now_ts - t < 60]
    if len(_gemini_calls) >= 14:
        wait = 60 - (now_ts - _gemini_calls[0]) + 1
        print(f"    ⏳ Gemini rate limit — waiting {wait:.0f}s")
        time.sleep(wait)
    _gemini_calls.append(time.time())

# ─── State management ───
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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

def fetch_page(url, extra_headers=None, proxy=None):
    """Fetch a URL and return a BeautifulSoup object, or None on error."""
    hdrs = {**HEADERS, **(extra_headers or {})}
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
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")
    except Exception as e:
        print(f"    Error fetching {url}: {e}")
        return None

def extract_text(soup, selector=""):
    """Extract cleaned text from a BeautifulSoup object, optionally within a CSS selector."""
    for tag in soup(["script", "style", "nav", "footer", "header", "noscript"]):
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

    soup = fetch_page(listing_url, proxy=site.get("proxy"))
    if not soup:
        return None

    if not link_selector:
        text = extract_text(soup, selector)
        if len(text) < 50:
            print(f"    ⚠️ Very little content extracted ({len(text)} chars) — site may require JavaScript rendering")
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        return {"type": "hash_check", "text": text, "hash": text_hash}

    if location_filter:
        anchors = []
        for section in soup.select("section.openings-section"):
            header = section.select_one("header, .opening-header")
            if header and location_filter.lower() in header.get_text().lower():
                anchors.extend(section.select(link_selector))
        print(f"    Filtered to {len(anchors)} links in '{location_filter}' sections")
    else:
        anchors = soup.select(link_selector)
    if not anchors and len(extract_text(soup, selector)) < 100:
        print(f"    ⚠️ No job links found and page content is minimal — likely JS-rendered")

    all_urls = set()
    jobs = []
    seen_in_batch = set()
    for a in anchors:
        href = a.get("href", "").strip()
        if not href or href == "#" or href == listing_url:
            continue

        title = a.get_text(strip=True) or "Untitled"
        if title.lower() in ("view job", "apply", "apply now", "learn more",
                              "read more", "click here", "view"):
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
        detail_soup = fetch_page(job["url"])
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
            resp = requests.post(api["url"], headers=api["headers"], json=body, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except Exception as e:
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
        resp = requests.get(api["url"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
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

        # Department filter: Greenhouse returns departments as a list of objects
        if department_filter:
            departments = posting.get("departments", [])
            dept_names = " ".join(d.get("name", "") for d in departments).lower()
            if department_filter.lower() not in dept_names:
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
        resp = requests.get(api["url"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
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
            detail_soup = fetch_page(job_url)
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
        resp = requests.get(api["url"], headers=HEADERS, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
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
            detail_soup = fetch_page(job_url)
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
        print("    No RSS URL configured for Taleo site.")
        return None

    try:
        resp = requests.get(rss_url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        root = ET.fromstring(resp.content)
    except Exception as e:
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
            detail_soup = fetch_page(link)
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
        resp = requests.get(api["url"], headers=hdrs, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
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
        print(f"    Pinpoint API error: {e}")
        return None

    postings = data.get(api.get("response_key", "data"), [])
    if not isinstance(postings, list):
        postings = []
    print(f"    API returned {len(postings)} jobs")

    new_jobs = []
    for posting in postings:
        title = posting.get(fields.get("title", "title"), "Untitled")
        job_url = posting.get(fields.get("url", "url"), "")
        location = posting.get(fields.get("location", "locationName"), "")
        department = posting.get(fields.get("department", "departmentName"), "")

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

    return {"total": len(postings), "new": new_jobs}

# ─── 9. PLAYWRIGHT (headless browser for JS-rendered sites) ───
def check_playwright(site, seen_urls):
    """Use headless Chromium to scrape JS-rendered job listing pages."""
    listing_url = site["url"]
    link_selector = site.get("link_selector", "")
    base_url = site.get("base_url", "")
    selector = site.get("selector", "")
    wait_selector = site.get("wait_selector", "")
    wait_ms = site.get("wait_ms", 5000)

    print(f"    Launching headless Chromium...")
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            page = browser.new_page()
            page.goto(listing_url, timeout=30000, wait_until="networkidle")
            if wait_selector:
                page.wait_for_selector(wait_selector, timeout=15000)
            else:
                page.wait_for_timeout(wait_ms)
            html = page.content()
            browser.close()
    except Exception as e:
        print(f"    Playwright error: {e}")
        return None

    soup = BeautifulSoup(html, "html.parser")

    # Hash-check mode (no link_selector)
    if not link_selector:
        text = extract_text(soup, selector)
        if len(text) < 50:
            print(f"    ⚠️ Very little content ({len(text)} chars)")
        text_hash = hashlib.sha256(text.encode()).hexdigest()
        return {"type": "hash_check", "text": text, "hash": text_hash}

    # Link-extraction mode
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
        if title.lower() in ("view job", "apply", "apply now", "learn more",
                              "read more", "click here", "view", "more info"):
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
        detail_soup = fetch_page(job["url"])
        detail_text = extract_text(detail_soup) if detail_soup else ""
        new_jobs.append({
            "title": job["title"],
            "url": job["url"],
            "detail_text": detail_text or f"Title: {job['title']} (detail page could not be loaded)"
        })

    return {"total": len(all_urls), "new": new_jobs}


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
}


# ═══════════════════════════════════════════════════════════════
#  AI MATCHING — send job details to Gemini
# ═══════════════════════════════════════════════════════════════

def evaluate_with_gemini(site_name, job_title, job_url, detail_text, is_page_level=False):
    gemini_rate_limit()
    url = (
        f"https://generativelanguage.googleapis.com/v1beta/"
        f"models/gemini-2.5-flash:generateContent?key={GEMINI_API_KEY}"
    )

    if is_page_level:
        prompt = f"""You are a job matching assistant. You will be given the full content of a careers page and a candidate's complete CV. Your task is to identify job listings on the page and assess each one against the candidate's profile.

INSTRUCTIONS:
1. Identify any individual job listings or vacancies mentioned on the page.
2. For each job found, assess alignment with the candidate's CV using these criteria:
   - Field alignment: Does the role relate to the candidate's areas of study and interest (international relations, international law, genocide/transitional justice, European foreign policy, security studies, higher education policy, policy research, sociology, history)?
   - Skills match: Does the candidate have the required or comparable skills (research, data analysis, policy writing, SPSS, Python, multilingual)?
   - Experience level: Is the role appropriate for someone with an MSc in progress, a strong BA, and research/admin assistant experience — but no full-time professional experience yet?
3. Rate each job: High, Medium, or Low.
4. Only include jobs rated High or Medium.
5. If no jobs are found or none match, respond with exactly: NO_MATCH

Note: Do NOT factor location into the match rating. Location should be reported in the output for the candidate's information, but a role in another city or country should not lower the rating.

FORMAT (for each matching job — use this exact format with these exact labels):
JOB: [Job title]
ORGANISATION: {site_name}
LOCATION: [City/country, or "Remote" if applicable — extract from page if possible, otherwise write "Not specified"]
TYPE: [Full-time/Part-time/Internship/Contract — extract from page if possible, otherwise write "Not specified"]
DEADLINE: [Application deadline if mentioned, or "Not specified"]
SALARY: [Salary or pay range if mentioned, or "Not specified"]
MATCH: [High/Medium]
FIELD: [1-5 score for field alignment, where 5 = perfect match to candidate's interests]
SKILLS: [1-5 score for skills match, where 5 = candidate has all required skills]
SENIORITY: [1-5 score for seniority fit, where 5 = perfect level for the candidate, 1 = far too senior or too junior]
REASON: [2-3 sentences explaining the match and any notable gaps]
URL: {job_url}

IMPORTANT:
- The page content may be in German or another language — assess it regardless.
- Err on the side of inclusion: if a role is plausibly relevant, rate it Medium rather than Low.
- Pay close attention to seniority requirements — roles requiring 5+ years of experience should be rated Low.

---

CAREERS PAGE CONTENT:
{detail_text[:10000]}

---

CANDIDATE CV:
{CANDIDATE_CV}
"""
    else:
        prompt = f"""You are a job matching assistant. You will be given a full job description and a candidate's complete CV. Assess how well this specific role matches the candidate.

ASSESSMENT CRITERIA:
1. Field alignment: Does the role relate to the candidate's areas of study and interest (international relations, international law, genocide/transitional justice, European foreign policy, security studies, higher education policy, policy research, sociology, history)?
2. Skills match: Does the candidate have the required or comparable skills (research, data analysis, policy writing, SPSS, Python, multilingual)?
3. Experience level: Is the role appropriate for someone with an MSc in progress, a strong BA, and research/admin assistant experience — but no full-time professional experience yet? Roles requiring 5+ years of professional experience should be rated Low.

RATING SCALE:
- High: Strong alignment in field, skills, and seniority. The candidate is a competitive applicant.
- Medium: Plausible fit — the candidate could apply with some stretch, or the role is adjacent to their expertise.
- Low: Poor fit due to field mismatch or excessive seniority requirements.

Note: Do NOT factor location into the match rating. Location should be reported in the output for the candidate's information, but a role in another city or country should not lower the rating.

If the match is High or Medium, respond in this exact format (use these exact labels):
JOB: {job_title}
ORGANISATION: {site_name}
LOCATION: [City/country as stated in the description, or "Not specified"]
TYPE: [Full-time/Part-time/Internship/Contract as stated, or "Not specified"]
DEADLINE: [Application deadline if mentioned, or "Not specified"]
SALARY: [Salary or pay range if mentioned, or "Not specified"]
MATCH: [High/Medium]
FIELD: [1-5 score for field alignment, where 5 = perfect match to candidate's interests]
SKILLS: [1-5 score for skills match, where 5 = candidate has all required skills]
SENIORITY: [1-5 score for seniority fit, where 5 = perfect level for the candidate, 1 = far too senior or too junior]
REASON: [2-3 sentences explaining the match and any notable gaps]
URL: {job_url}

If the match is Low, respond with exactly: NO_MATCH

IMPORTANT:
- The job description may be in German or another language — assess it regardless.
- Err on the side of inclusion for genuinely relevant roles.

---

JOB DESCRIPTION:
{detail_text[:10000]}

---

CANDIDATE CV:
{CANDIDATE_CV}
"""

    payload = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": 0.2, "maxOutputTokens": 8192}
    }

    try:
        resp = requests.post(url, json=payload, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        result = data["candidates"][0]["content"]["parts"][0]["text"]
        print(f"    --- Gemini raw response ---")
        print(result)
        print(f"    --- End Gemini response ---")
        return result
    except Exception as e:
        print(f"    Gemini error: {e}")
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
    if not raw_text or "NO_MATCH" in raw_text:
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
        print("🏃 DRY RUN — populating state only, no Gemini calls or notifications\n")
    print()

    state = load_state()
    all_matches = []
    errors = []
    empty_sites = []

    for site in SITES:
        name = site["name"]
        method = site["method"]
        site_key = site["url"]

        print(f"\n[{site.get('id', '?')}] {name} ({method})")

        if site_key not in state:
            state[site_key] = {"seen_urls": [], "last_checked": "", "listing_hash": ""}

        seen_urls = set(state[site_key].get("seen_urls", []))
        handler = METHOD_HANDLERS.get(method)

        if not handler:
            print(f"    Unknown method: {method}. Skipping.")
            continue

        result = handler(site, seen_urls)

        if result is None:
            errors.append(name)
            state[site_key]["last_checked"] = now.isoformat()
            continue

        # Handle the hash-check case (HTML sites without link_selector)
        if isinstance(result, dict) and result.get("type") == "hash_check":
            # Check for "no vacancies" pages
            page_lower = result["text"].lower()
            if any(phrase in page_lower for phrase in NO_VACANCY_PHRASES):
                print(f"    ℹ️ No vacancies listed (page says so).")
                empty_sites.append(name)
                state[site_key]["listing_hash"] = result["hash"]
                state[site_key]["last_checked"] = now.isoformat()
                continue

            old_hash = state[site_key].get("listing_hash", "")
            if result["hash"] != old_hash:
                if DRY_RUN:
                    print(f"    Page content changed (dry run — skipping Gemini)")
                else:
                    print(f"    Page content changed! Sending full text to Gemini...")
                    gemini_result = evaluate_with_gemini(name, f"Page update on {name}", site["url"], result["text"], is_page_level=True)
                    if gemini_result and "NO_MATCH" not in gemini_result:
                        parsed = parse_gemini_matches(gemini_result)
                        for m in parsed:
                            all_matches.append(format_match_for_telegram(m))
                state[site_key]["listing_hash"] = result["hash"]
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
                gemini_result = evaluate_with_gemini(name, job["title"], job["url"], job["detail_text"])

                if gemini_result is None:
                    # Gemini call failed — do NOT mark as seen, retry next run
                    print(f"        ⚠️ Gemini failed — will retry next run.")
                    continue

                if "NO_MATCH" not in gemini_result:
                    parsed = parse_gemini_matches(gemini_result)
                    for m in parsed:
                        all_matches.append(format_match_for_telegram(m))
                    print(f"        ✅ Match!")
                else:
                    print(f"        No match.")

            seen_urls.add(job["url"])
            for extra in job.get("_also_track", []):
                seen_urls.add(extra)
            time.sleep(1)

        state[site_key]["seen_urls"] = list(seen_urls)
        state[site_key]["last_checked"] = now.isoformat()

    # ─── Prune seen_urls to prevent state.json bloat ───
    for site_key, site_state in state.items():
        seen = site_state.get("seen_urls", [])
        if len(seen) > 200:
            site_state["seen_urls"] = seen[-200:]
            print(f"  Pruned seen_urls for {site_key} to last 200 entries")

    # ─── Save state ───
    save_state(state)

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

    if errors and not DRY_RUN:
        error_msg = f"⚠️ <b>Job Monitor Errors</b>\nFailed to scrape: {escape_html(', '.join(errors))}"
        send_telegram(error_msg)

    if errors:
        print(f"Errors: {', '.join(errors)}")
    if empty_sites:
        print(f"Empty: {', '.join(empty_sites)}")


if __name__ == "__main__":
    main()

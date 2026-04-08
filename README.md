# Job Monitor

Automated job scraper that monitors career pages across organisations, matches new postings against a candidate CV using Google Gemini, and sends relevant matches to Telegram.

Runs as a GitHub Actions workflow, triggered on a schedule via [cron-job.org](https://cron-job.org) or manual dispatch.

## How It Works

1. **Scrape** — Checks career pages using method-specific handlers (HTML scraping, Workday/Greenhouse/Workable/Personio APIs, RSS feeds, Playwright for JS-rendered sites)
2. **Detect new postings** — Compares against `state.json` to identify jobs not seen in previous runs. For sites without individual job links, uses content hashing to detect page changes.
3. **Evaluate** — Sends each new job's full description + the candidate CV to Gemini 2.5 Flash, which rates field alignment, skills match, and seniority fit
4. **Notify** — Formats High/Medium matches and sends them to Telegram with scores, metadata, and direct links

## Supported Scraping Methods

| Method | Notes |
|---|---|
| `html` | CSS selector-based; falls back to content hashing when no link selector is configured |
| `workday_api` | Paginated POST API with optional detail fetching |
| `greenhouse_api` | JSON API with inline descriptions via `?content=true` |
| `workable_api` | Widget API with detail page fetching |
| `personio_xml` | XML feed parsing |
| `taleo_rss` | RSS feed parsing |
| `palladium_api` | Custom AJAX endpoint with country filtering |
| `pinpoint_api` | Uses `cloudscraper` for Cloudflare bypass |
| `playwright` | Headless Chromium for JS-rendered listings |
| `oracle_hcm_api` | Oracle HCM Cloud REST API |
| `hireserve_api` | Hireserve ATS JSON feed with category filtering |

## Project Structure

```
├── monitor.py                      # Main scraping + matching pipeline
├── state.json                      # Persistent state tracking seen job URLs and content hashes
├── dry_run.txt                     # Set to "true" to populate state without Gemini calls or notifications
├── requirements.txt                # Python dependencies
└── .github/workflows/
    └── check-jobs.yml              # GitHub Actions workflow
```

Sensitive files (`config.json` and `cv.txt`) are stored as GitHub secrets and written to the runner at build time.

## Setup

### Prerequisites

- Python 3.12+
- A Telegram bot token and chat ID
- A Google Gemini API key
- (Optional) A Cloudflare Worker URL + token for proxied fetches

### Installation

```bash
pip install -r requirements.txt
playwright install chromium --with-deps
```

### GitHub Secrets

| Secret | Required | Description |
|---|---|---|
| `CONFIG_JSON` | Yes | Full contents of `config.json` (site definitions + qualifications) |
| `CV_TEXT` | Yes | Full contents of `cv.txt` (candidate CV in plain text) |
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | Yes | Target chat/channel ID for notifications |
| `GEMINI_API_KEY` | Yes | Google Gemini API key |
| `CF_WORKER_URL` | No | Cloudflare Worker proxy URL |
| `CF_WORKER_TOKEN` | No | Auth token for the Cloudflare Worker |

### Configuration

#### `config.json`

Contains a `qualifications` string (used in the Gemini prompt) and a `sites` array. Each site entry needs at minimum:

```json
{
  "id": 1,
  "name": "Example Org",
  "url": "https://example.org/careers",
  "method": "html",
  "selector": "div.job-listings",
  "link_selector": "a[href*='/jobs/']",
  "base_url": "https://example.org"
}
```

For API-based sites, include an `api` object with the endpoint URL, HTTP method, headers, and response field mappings.

#### Filtering

- `location_filter` — Only include jobs matching a location string
- `department_filter` — Only include jobs in a specific department (Greenhouse)
- `country_filter` — Filter by country code (Palladium)

### Running

```bash
# First run: populate state without sending notifications
echo "true" > dry_run.txt
python monitor.py

# Subsequent runs: evaluate new jobs and notify
echo "false" > dry_run.txt
python monitor.py
```

The GitHub Actions workflow handles this automatically — it writes `config.json` and `cv.txt` from secrets, runs the monitor, and commits the updated `state.json`.

## Matching

Gemini evaluates jobs on three dimensions (each scored 1–5):

- **Field alignment** — Relevance to the candidate's areas of study and interest
- **Skills match** — Whether the candidate has required or comparable skills
- **Seniority fit** — Whether the role suits the candidate's experience level

Jobs rated High or Medium are sent as Telegram notifications. Location is reported but does not affect the match rating.

## Rate Limiting

The free Gemini tier allows 15 requests per minute. The monitor enforces a 14 RPM ceiling with automatic backoff.

## State Management

`state.json` tracks seen job URLs per site and content hashes for hash-check sites. Seen URLs are pruned to the most recent 200 per site to prevent unbounded growth.

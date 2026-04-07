# Job Monitor

Automated job scraper that monitors career pages across think tanks, NGOs, universities, and consultancies, matches new postings against a candidate CV using Gemini, and sends relevant matches to Telegram.

Runs as a GitHub Actions workflow on a schedule.

## How It Works

1. **Scrape** — Checks 36 career pages using method-specific handlers (HTML scraping, Workday/Greenhouse/Workable/Personio APIs, RSS feeds, Playwright for JS-rendered sites)
2. **Detect new postings** — Compares against `state.json` to identify jobs not seen in previous runs. For sites without individual job links, uses content hashing to detect page changes.
3. **Evaluate** — Sends each new job's full description + the candidate CV to Gemini 2.5 Flash, which rates field alignment, skills match, and seniority fit
4. **Notify** — Formats High/Medium matches and sends them to Telegram with scores, metadata, and direct links

## Supported Scraping Methods

| Method | Sites | Notes |
|---|---|---|
| `html` | Static career pages (IPPR, Chatham House, jobs.ac.uk, etc.) | CSS selector-based; falls back to content hashing when no link selector is configured |
| `workday_api` | Open Society Foundations, Pew Trusts | Paginated POST API with optional detail fetching |
| `greenhouse_api` | Portland Communications, FSG, Anthropic | JSON API with inline descriptions via `?content=true` |
| `workable_api` | Hakluyt, RAND Europe | Widget API with detail page fetching |
| `personio_xml` | ECFR | XML feed parsing |
| `taleo_rss` | Bridgespan Group | RSS feed parsing |
| `palladium_api` | Palladium Group | Custom AJAX endpoint with country filtering |
| `pinpoint_api` | ODI Global | Uses `cloudscraper` for Cloudflare bypass |
| `playwright` | SOAS, IISS | Headless Chromium for JS-rendered listings |

## Project Structure

```
├── monitor.py          # Main scraping + matching pipeline
├── config.json         # Site definitions, API configs, and candidate qualifications
├── cv.txt              # Candidate CV (plain text)
├── state.json          # Persistent state tracking seen job URLs and content hashes
├── dry_run.txt         # Set to "true" to populate state without Gemini calls or notifications
├── requirements.txt    # Python dependencies
└── .github/workflows/
    └── check-jobs.yml  # GitHub Actions workflow
```

## Setup

### Prerequisites

- Python 3.12+
- A Telegram bot token and chat ID
- A Gemini API key
- (Optional) A Cloudflare Worker URL + token for proxied fetches

### Installation

```bash
pip install -r requirements.txt
playwright install chromium --with-deps
```

### Environment Variables

| Variable | Required | Description |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | Yes | Telegram Bot API token |
| `TELEGRAM_CHAT_ID` | Yes | Target chat/channel ID for notifications |
| `GEMINI_API_KEY` | Yes | Google Gemini API key |
| `CF_WORKER_URL` | No | Cloudflare Worker proxy URL (for sites that block direct requests) |
| `CF_WORKER_TOKEN` | No | Auth token for the Cloudflare Worker |

For GitHub Actions, add these as repository secrets.

### Running Locally

```bash
# First run: populate state without sending notifications
echo "true" > dry_run.txt
python monitor.py

# Subsequent runs: evaluate new jobs and notify
echo "false" > dry_run.txt
python monitor.py
```

### GitHub Actions

The workflow is configured for manual dispatch (`workflow_dispatch`). To add a schedule, edit `.github/workflows/check-jobs.yml`:

```yaml
on:
  workflow_dispatch:
  schedule:
    - cron: '0 8 * * *'  # daily at 08:00 UTC
```

The workflow automatically commits updated `state.json` after each run.

## Configuration

### Adding a New Site

Add an entry to the `sites` array in `config.json`. The `method` field determines which handler is used. At minimum:

```json
{
  "id": 37,
  "name": "Example Org",
  "url": "https://example.org/careers",
  "method": "html",
  "selector": "div.job-listings",
  "link_selector": "a[href*='/jobs/']",
  "base_url": "https://example.org"
}
```

For API-based sites, include an `api` object with the endpoint URL, HTTP method, headers, and response field mappings. See existing entries in `config.json` for examples of each method type.

### Filtering

- `location_filter` — Only include jobs matching a location string
- `department_filter` — Only include jobs in a specific department (Greenhouse)
- `country_filter` — Filter by country code (Palladium)

### Matching Criteria

Gemini evaluates jobs on three dimensions (each scored 1–5):

- **Field alignment** — Relevance to the candidate's areas of study and interest
- **Skills match** — Whether the candidate has required or comparable skills
- **Seniority fit** — Whether the role suits the candidate's experience level

Jobs rated High or Medium are sent as notifications. Location is reported but does not affect the match rating.

## Gemini Rate Limiting

The free Gemini tier allows 15 requests per minute. The monitor enforces a 14 RPM ceiling with automatic backoff.

## State Management

`state.json` tracks seen job URLs per site and content hashes for hash-check sites. Seen URLs are pruned to the most recent 200 per site to prevent unbounded growth. The file is committed back to the repo after each GitHub Actions run.

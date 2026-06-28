"""
Daily job report generator.
Writes JSON files to site/data/ for the Cloudflare Pages dashboard.
"""

import json
import os
from datetime import datetime, timezone


SITE_DATA_DIR = os.environ.get("SITE_DATA_DIR", os.path.join("site", "data"))
DATES_FILE = os.path.join(SITE_DATA_DIR, "dates.json")
MAX_DAYS = 90  # Keep at most 90 days of reports


def _ensure_dir():
    os.makedirs(SITE_DATA_DIR, exist_ok=True)


def load_dates():
    """Load the list of available report dates."""
    if os.path.exists(DATES_FILE):
        with open(DATES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_report(jobs, now=None):
    """
    Append today's jobs to the daily report file and update dates.json.

    Each call merges into the existing file for today (so all three daily
    runs accumulate into one file). Deduplication is by URL.

    Args:
        jobs: list of dicts with keys:
            title, organisation, url, match (high/medium/low/none),
            field_score, skills_score, seniority_score, reason,
            location, type, deadline, salary
        now: optional datetime (for testing)
    """
    _ensure_dir()
    if now is None:
        now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    report_file = os.path.join(SITE_DATA_DIR, f"{date_str}.json")

    # Load existing report for today (from earlier runs)
    existing_jobs = []
    run_count = 0
    if os.path.exists(report_file):
        with open(report_file, "r", encoding="utf-8") as f:
            existing = json.load(f)
            existing_jobs = existing.get("jobs", [])
            run_count = existing.get("run_count", 0)

    # Merge: deduplicate by (URL, title). Page-level / hash-check sites list
    # several jobs at ONE careers-page URL, so deduping on URL alone collapses
    # them to a single entry and hides the rest (including real matches).
    def _dedup_key(j):
        return (j.get("url", ""), (j.get("title", "") or "").strip().lower())
    seen_keys = {_dedup_key(j) for j in existing_jobs if j.get("url")}
    for job in jobs:
        if job.get("url") and _dedup_key(job) not in seen_keys:
            existing_jobs.append(job)
            seen_keys.add(_dedup_key(job))

    run_count += 1

    report = {
        "date": date_str,
        "generated_at": now.isoformat(),
        "run_count": run_count,
        "jobs": existing_jobs,
    }

    with open(report_file, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)

    # Update dates index
    dates = load_dates()
    if date_str not in dates:
        dates.append(date_str)
    dates.sort()

    # Prune old dates and their files
    if len(dates) > MAX_DAYS:
        for old_date in dates[:-MAX_DAYS]:
            old_file = os.path.join(SITE_DATA_DIR, f"{old_date}.json")
            if os.path.exists(old_file):
                os.remove(old_file)
        dates = dates[-MAX_DAYS:]

    with open(DATES_FILE, "w", encoding="utf-8") as f:
        json.dump(dates, f)

    print(f"  📊 Daily report: {len(existing_jobs)} jobs in {report_file} (run {run_count})")

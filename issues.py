"""
Issue tracking for the job monitor.

Writes a single JSON file (issues.json) at the repo root that accumulates
issues across runs, so persistent problems can be surfaced without trawling
through workflow logs. The file is committed back to the repo by the
GitHub Actions workflow, so history persists across the ephemeral runners.

Structure:
{
  "last_updated": "2026-04-29T12:31:00+00:00",
  "site_summary": {
    "<site name>": {
      "site_id": 47,
      "url": "...",
      "method": "html",
      "consecutive_failures": 16,
      "total_recent_failures": 48,
      "first_recent_failure": "...",
      "last_recent_failure": "...",
      "currently_paused": true,
      "paused_until": "...",
      "last_type": "fetch_error",
      "last_message": "..."
    }
  },
  "issues": [
    {
      "timestamp": "...",
      "site": "...",
      "site_id": 47,
      "url": "...",
      "method": "html",
      "type": "fetch_error" | "site_paused" | "gemini_call_failed"
            | "gemini_parse_failed" | "unknown_method",
      "message": "...",
      ...optional extras (job_title, job_url, consecutive_count, etc.)
    }
  ]
}

Only sites with active issues (consecutive_errors > 0 or currently paused)
appear in `site_summary`. The full chronological log lives in `issues`.
"""

import json
import os
from datetime import datetime, timedelta


ISSUES_FILE = "issues.json"
MAX_ISSUE_AGE_DAYS = 30


def load():
    """Load the existing issues file or return a fresh structure."""
    if os.path.exists(ISSUES_FILE):
        try:
            with open(ISSUES_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                data.setdefault("last_updated", "")
                data.setdefault("site_summary", {})
                data.setdefault("issues", [])
                return data
        except (json.JSONDecodeError, IOError) as e:
            print(f"  ⚠️ Could not read {ISSUES_FILE} ({e}); starting fresh")
    return {"last_updated": "", "site_summary": {}, "issues": []}


def add(issues_data, now, site, issue_type, message, **extra):
    """
    Append a single issue entry to the chronological list.

    `site` should be the dict from config (provides name, id, url, method).
    `extra` covers per-type fields like job_title, job_url, consecutive_count.
    """
    entry = {
        "timestamp": now.isoformat(),
        "site": site.get("name", "?"),
        "site_id": site.get("id"),
        "url": site.get("url", ""),
        "method": site.get("method", ""),
        "type": issue_type,
        "message": (message or "").strip(),
    }
    entry.update(extra)
    issues_data["issues"].append(entry)


def _prune_old(issues_data, now, max_days=MAX_ISSUE_AGE_DAYS):
    """Drop issue entries older than max_days."""
    cutoff = now - timedelta(days=max_days)
    kept = []
    for entry in issues_data["issues"]:
        ts_str = entry.get("timestamp", "")
        try:
            ts = datetime.fromisoformat(ts_str)
            if ts > cutoff:
                kept.append(entry)
        except ValueError:
            # Keep entries with malformed timestamps rather than silently drop
            kept.append(entry)
    issues_data["issues"] = kept


def _rebuild_summary(issues_data, state):
    """
    Rebuild the per-site rollup from the current run's state and recent issues.

    Only includes sites that are *currently* problematic — consecutive_errors > 0
    or paused. Sites that recovered are absent from summary but their history
    remains in `issues`.
    """
    summary = {}

    # Group issues by URL once
    by_url = {}
    for entry in issues_data["issues"]:
        by_url.setdefault(entry.get("url", ""), []).append(entry)

    for site_key, site_state in state.items():
        consecutive = site_state.get("consecutive_errors", 0)
        paused_until = site_state.get("paused_until", "")

        if consecutive == 0 and not paused_until:
            continue

        recent = by_url.get(site_key, [])
        if not recent:
            continue  # No recent issue entries, can't build a meaningful row

        recent_sorted = sorted(recent, key=lambda e: e.get("timestamp", ""))
        latest = recent_sorted[-1]

        summary[latest.get("site", site_key)] = {
            "site_id": latest.get("site_id"),
            "url": site_key,
            "method": latest.get("method", ""),
            "consecutive_failures": consecutive,
            "total_recent_failures": len(recent_sorted),
            "first_recent_failure": recent_sorted[0].get("timestamp", ""),
            "last_recent_failure": latest.get("timestamp", ""),
            "currently_paused": bool(paused_until),
            "paused_until": paused_until,
            "last_type": latest.get("type", ""),
            "last_message": latest.get("message", ""),
        }

    issues_data["site_summary"] = summary


def finalize(issues_data, state, now):
    """Prune old entries, rebuild summary, update timestamp, write to disk."""
    _prune_old(issues_data, now)
    _rebuild_summary(issues_data, state)
    issues_data["last_updated"] = now.isoformat()
    with open(ISSUES_FILE, "w", encoding="utf-8") as f:
        json.dump(issues_data, f, indent=2, ensure_ascii=False)
    n_issues = len(issues_data["issues"])
    n_sites = len(issues_data["site_summary"])
    print(
        f"  📝 Issues log: {n_issues} entries (last {MAX_ISSUE_AGE_DAYS}d), "
        f"{n_sites} site(s) with active issues"
    )

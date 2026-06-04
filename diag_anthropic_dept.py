"""
diag_anthropic_dept.py — find the REAL Greenhouse department label(s) for
Anthropic's London jobs, so you can decide whether to re-add a *correct*
department_filter or leave it off.

Background:
    The pipeline's Anthropic entry surfaced ZERO jobs for ~53 days. Cause was
    a department_filter of "Public Policy" being substring-matched against
    Greenhouse's departments[].name — and that exact label evidently doesn't
    exist on the board, so every job was silently dropped. The filter has been
    REMOVED in config.json (London location_filter + the LLM rubric do the
    narrowing now). This script lets you confirm the real labels and, if you
    want policy-only filtering back, gives you the exact string(s) to use with
    the now list-capable department_filter.

Usage (Windows PowerShell):
    python diag_anthropic_dept.py

No secrets, no monitor import — just hits the public Greenhouse board API.
Needs only `requests` (already in requirements.txt).
"""

import sys
from collections import Counter

import requests

BOARD_URL = "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true"
LOCATION_NEEDLE = "london"  # same case-insensitive substring test the pipeline uses


def main():
    print(f"GET {BOARD_URL}")
    try:
        r = requests.get(BOARD_URL, timeout=30)
        r.raise_for_status()
        jobs = r.json().get("jobs", [])
    except Exception as e:
        print(f"FAILED to fetch board: {e}")
        sys.exit(1)

    print(f"Board returned {len(jobs)} total jobs.\n")

    london, dept_counter, dept_per_job = [], Counter(), []
    for j in jobs:
        loc = (j.get("location") or {}).get("name", "") or ""
        if LOCATION_NEEDLE in loc.lower():
            depts = [d.get("name", "") for d in j.get("departments", []) if d.get("name")]
            offices = [o.get("name", "") for o in j.get("offices", []) if o.get("name")]
            london.append((j.get("title", "Untitled"), loc, depts, offices))
            for d in depts:
                dept_counter[d] += 1
            dept_per_job.append(depts)

    if not london:
        print("No jobs whose location contains 'London'.")
        print("That likely means the London location_filter itself is too strict")
        print("(check the exact location strings below) OR there are genuinely")
        print("no London roles open right now.\n")
        print("First 15 location strings on the board (to eyeball the format):")
        for j in jobs[:15]:
            print(f"  - {(j.get('location') or {}).get('name', '')!r}  | {j.get('title','')}")
        return

    print(f"=== {len(london)} job(s) with 'London' in location ===\n")
    for title, loc, depts, offices in london:
        print(f"  • {title}")
        print(f"        location:    {loc!r}")
        print(f"        departments: {depts}")
        print(f"        offices:     {offices}")

    print("\n=== Distinct department labels across London jobs (with counts) ===")
    for name, n in dept_counter.most_common():
        print(f"  {n:>3}x  {name!r}")

    # Heuristic suggestion: which labels look policy/affairs-ish?
    policyish = sorted({
        name for name in dept_counter
        if any(k in name.lower() for k in ("policy", "affairs", "government", "public"))
    })

    print("\n=== Recommendation ===")
    if not policyish:
        print("None of the London department labels look policy-specific.")
        print("Keep department_filter OFF (current config) and let the LLM rubric")
        print("decide relevance — re-adding a filter would only risk dropping good roles.")
    else:
        as_list = "[" + ", ".join(f'"{p}"' for p in policyish) + "]"
        print("If you want to restrict to policy-type roles, the filter now accepts a")
        print("list and matches if ANY value is a substring of the department names.")
        print("Add this to the Anthropic site in config.json:\n")
        print(f'    "department_filter": {as_list}')
        print("\nBut note: the London location_filter + LLM rubric already keep noise")
        print("low (Telegram only fires on High/Medium matches), so OFF is also fine.")


if __name__ == "__main__":
    main()

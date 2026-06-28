#!/usr/bin/env python3
"""
One-time state migration for the DE / London config split.

WHY: each lane now reads its own state file. If those start empty, the first run
of each lane treats every current posting as "new" and re-notifies everything you
already saw — a duplicate burst. This splits your CURRENT state.json by site URL
(the key monitor.py uses) into state-de.json + state-london.json, so both lanes
start fully seeded and nothing re-fires.

HOW TO RUN (locally, Windows `py` launcher):
  1. Put these three files in one folder next to this script:
       - config-de.json
       - config-london.json
       - state.json          (download the CURRENT one from the repo: the file
                               at the repo root, "Raw" -> Save As state.json)
  2. Run:   py split_state.py
  3. Commit the two outputs (state-de.json, state-london.json) to the repo root.

Re-runnable and read-only on inputs; it only writes the two output files.
"""
import json, os, sys

HERE = os.path.dirname(os.path.abspath(__file__))

def load(name):
    path = os.path.join(HERE, name)
    if not os.path.exists(path):
        sys.exit(
            f"ERROR: '{name}' not found next to this script.\n"
            f"Place config-de.json, config-london.json and the current state.json "
            f"in this folder first, then re-run."
        )
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def urls_of(cfg_name):
    return {s["url"] for s in load(cfg_name)["sites"] if "url" in s}

def main():
    de_urls     = urls_of("config-de.json")
    london_urls = urls_of("config-london.json")
    overlap = de_urls & london_urls
    if overlap:
        print(f"⚠️  {len(overlap)} URL(s) appear in BOTH configs (a site is in both "
              f"lanes). Their state will go to DE. Fix the configs if unintended:")
        for u in sorted(overlap):
            print(f"     {u}")

    state = load("state.json")
    if not isinstance(state, dict):
        sys.exit("ERROR: state.json is not a JSON object keyed by URL — unexpected shape.")

    state_de, state_london, orphans = {}, {}, []
    for url, entry in state.items():
        if url in de_urls:
            state_de[url] = entry
        elif url in london_urls:
            state_london[url] = entry
        else:
            orphans.append(url)

    with open(os.path.join(HERE, "state-de.json"), "w", encoding="utf-8") as f:
        json.dump(state_de, f, ensure_ascii=False, indent=2)
    with open(os.path.join(HERE, "state-london.json"), "w", encoding="utf-8") as f:
        json.dump(state_london, f, ensure_ascii=False, indent=2)

    print(f"\nstate.json            : {len(state)} entries")
    print(f"  -> state-de.json     : {len(state_de)} entries  ({len(de_urls)} DE sites configured)")
    print(f"  -> state-london.json : {len(state_london)} entries  ({len(london_urls)} London/rest sites configured)")
    if orphans:
        print(f"\n{len(orphans)} state entr(y/ies) matched NEITHER config "
              f"(removed/renamed sites) — dropped from both:")
        for u in orphans:
            print(f"  - {u}")
    print("\n✅ Done. Commit state-de.json and state-london.json to the repo root, "
          "then deploy the split. Both lanes will start seeded — no re-burst.")

if __name__ == "__main__":
    main()

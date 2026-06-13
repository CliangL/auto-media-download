# New-Show Premiere: Total-Episode Count Detection Lag

## Symptom
A TV drama premieres on day X. The user searches for it and selects a xiaoya source.
`decide-tracking.py` returns `needs_total`. The drama's STRM files are generated but
the entry is **not** written to `drama-state.json` (handle-selection.sh exits code 2).

## Root Cause
The show just premiered. Web search engines have not yet indexed the episode count.
This is transient — the data exists on the official platform, just not crawled yet.

## Resolution
1. **Retry later**: Wait a few hours and re-run `decide-tracking.py`.
2. **Manual override**: If user knows the correct total:
   - Write to `known-totals.json`
   - Add drama to `drama-state.json` with correct total
   - Run `sync-drama-state.py push`
3. **decide-tracking.py behaviour**: The script retries Baidu → Sogou → Brave → TMDB.
   The issue is timing, not logic.

## Source Discrepancies
- Different platforms report different totals (iQiyi: 42, TVMao: 40, Sogou: 40)
- Prefer the user's source (typically the official streaming platform)

## Historical Example
- **家业** (2026-05-17 premiere): Script returned `needs_total` on first run;
  user confirmed 42 episodes (iQiyi). Manual correction applied.

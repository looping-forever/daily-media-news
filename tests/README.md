# tests/fixtures

Static fixtures + expected-results manifest for the daily-media-news pipeline
scripts. All fixture inputs are hand-written JSON/YAML; no test framework or
third-party dependency is required.

## Contents

| File | Purpose |
|------|---------|
| `manifest.json` | Machine-readable expectations (exit codes, survivors, drops+reasons, email assertions, history counts) |
| `config/interests.yml` | Test-only mirror of `config/interests.yml` (excluded_keywords block) |
| `input_full_matrix.json` | 35 stories covering spoilers, crime filter, excluded keywords, Nepal/Example City/TU/Example College/bank keep-drop cases, sort + cap-15 |
| `input_dedup_cases.json` | Same-URL pair, high title-overlap pair, fresh/stale history matches |
| `input_history_dedup.json` | Legacy-format history seed (`{title,url,date}`) for the dedup case |
| `input_excluded_only.json` | Single LEGO story → exit 2 with empty payload |
| `input_malformed.json` | 6 schema-violating stories (dropped individually) + 1 valid survivor |
| `input_empty.json` | Zero stories → exit 2, out-payload still written |
| `input_invalid_json.json` | Truncated JSON → fatal exit 1, no output written |
| `input_limited.json` | `research_status: limited` passthrough + XSS canary title |
| `history_prune_seed.json` | Mixed legacy/current history for prune test |
| `input_event_run1.json` / `input_event_run2.json` | Same event (`event_key`), different URL + reworded copy: labels NEW then UPDATED, single state event; run 2 doubles as the same-day re-report case |
| `input_similar_titles.json` | Similar titles that stay separate events (overlap below dedup/merge thresholds) |
| `input_deadline_future.json` | Future deadline + old pubDate survives (deadline freshness exemption) |
| `input_deadline_expired.json` + `state_deadline_seed.json` | Expired deadline: story dropped (`deadline_expired`), seeded event transitions to RESOLVED |
| `input_relevance_sort.json` | `personal_relevance` tie-break (absent defaults to priority) + range-violation schema drop |

## Running

The manifest is consumed by a throwaway verification runner that is NOT part of
the repo (keeps the repo stdlib-only and script-free). To verify manually:

```powershell
py scripts\validate_stories.py --in tests\fixtures\input_full_matrix.json `
  --out "$env:TEMP\v.json" --history "$env:TEMP\none.json" `
  --config-dir tests\fixtures\config
py scripts\build_email.py --in "$env:TEMP\v.json" `
  --out-html "$env:TEMP\e.html" --out-text "$env:TEMP\e.txt" `
  --template templates\email.html
```

Then compare survivors/drops against `manifest.json`.

## Date caveat

History fixtures use FIXED dates relative to authoring time (2026-08-25):
recent = 2026-08-28 (within 7-day TTL), stale = 2026-07-20, pruned = 2026-07-01.
`state_deadline_seed.json` and `input_deadline_expired.json` use deadline
2026-08-15 (must stay clearly before "yesterday"); `input_deadline_future.json`
uses deadline 2027-12-31 (must stay in the future). If you run this suite much
later, refresh those dates so the relationships hold.

## Fixture design notes

- The three "promo/unrelated" drop cases (Example College promo event, bank CSR promo,
  TU LEGO research) are realized via the **excluded_keywords** rule — the
  validator spec has no dedicated promo filter, so these fixtures carry the
  excluded keyword to produce a deterministic DROP with a recorded reason.
- "Road closure KEEP" exercises the crime-filter impact-marker exception:
  crime word (`assault`) co-occurs with `highway/closed/disrupted`.
- "Political drama DROP" and "minor accident DROP" contain crime words with no
  impact markers.
- "Veteran Nepali Folk Singer Dies at 80" proves the non-entertainment
  high-sensitivity spoiler veto (`dies`) fires outside entertainment categories.

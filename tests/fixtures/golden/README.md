# Golden Game Fixture — 0042500121

Canonical parity fixture per DESIGN.md §Testing. Every pipeline change must reproduce
identical enriched output from these frozen inputs.

## Game

- **NBA game ID:** `0042500121` — 2026 Eastern Conference First Round, Game 1
- **Teams:** Atlanta Hawks @ New York Knicks (MSG)
- **Date:** 2026-04-18 (tip-off 22:19 UTC), ended 2026-04-19 00:50 UTC
- **Final:** NYK 113 — ATL 102
- **Kalshi event code:** `26APR18ATLNYK`

## Hand-verified anchors (vs ESPN gameId 401869189 / Basketball-Reference 202604180NYK)

- Final score 113–102, Knicks home ✓
- Jalen Brunson 28 PTS / 7 AST; Karl-Anthony Towns 25 PTS; Mikal Bridges 11 PTS ✓
- Jalen Johnson 23 PTS; Onyeka Okongwu 19 PTS (ATL) ✓
- First action: Period Start 22:19:18 UTC; jump ball Towns vs Okongwu, tip to Hart ✓

## Files

| File | Records | Coverage |
|------|---------|----------|
| `nba_pbp_0042500121.jsonl.gz` | 548 actions | Full game, 22:19 → 00:50 UTC |
| `kalshi_trades_0042500121.jsonl.gz` | 20,376 trades | **Partial: 00:15 → 00:51 UTC only** (~Q4 + endgame). Kalshi trade collection began mid-game on this first collection day |

The partial trade coverage is deliberate test surface: enriched output must include
trades correctly joined to late-game state, and the pipeline must handle the absence
of trades for the first ~2 hours without error.

## Regeneration

```bash
aws s3 cp s3://prediction-markets-data/bronze_merged/nba_cdn/nba_pbp_0042500121.jsonl.gz .
aws s3 cp s3://prediction-markets-data/bronze_merged/kalshi_ws/trade/date=2026-04-19/merged.jsonl.gz - \
  | gunzip | grep 26APR18ATLNYK | gzip > kalshi_trades_0042500121.jsonl.gz
```

Do **not** regenerate casually — these inputs are frozen. If bronze upstream changes,
the golden output file (added with the Flink job) must be re-verified by hand.

**History:** regenerated 2026-07-07 after fixing a lexicographic-vs-chronological
ordering bug in `scripts/merge_kalshi_ws.py` (S3 lists UUID-named files
alphabetically within each hour prefix; the original merge concatenated in that
order, shuffling ~60s flush chunks). Record count unchanged (20,376); order fixed.
The bug was caught by the replayer's ordering verification (`pm.replay.merge`).

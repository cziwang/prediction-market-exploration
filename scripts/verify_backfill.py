"""Post-backfill sanity checks (TESTING.md Layer 6).

Validates the full-dataset backfill in ClickHouse against independent ground
truth: the game map, and — the strongest check — settlement reality: at the
final buzzer the model's win probability must be exactly 1.0 for the side
that actually won (score_diff sign), for every game.

Usage:
    python scripts/verify_backfill.py
"""

import json
import sys
from pathlib import Path

import clickhouse_connect

GAME_MAP = json.loads(Path("reference/game_map.json").read_text())

failures: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  [{detail}]" if detail else ""))
    if not cond:
        failures.append(name)


def main() -> None:
    ch = clickhouse_connect.get_client(
        host="localhost", port=8123, username="pm", password="pm", database="pm"
    )

    print("== coverage ==")
    rows = ch.command("SELECT count() FROM enriched_trades")
    print(f"  total enriched rows: {rows:,}")

    present = {r[0] for r in ch.query("SELECT DISTINCT game_id FROM enriched_trades").result_rows}
    mapped = set(GAME_MAP.values())
    missing = sorted(mapped - present)
    unexpected = sorted(present - mapped)
    check("no unexpected game_ids", not unexpected, str(unexpected))
    print(f"  games present: {len(present)}/{len(mapped)}; missing: {missing or 'none'}")
    # Missing games are acceptable ONLY if they plausibly predate trade
    # collection or fall in coverage gaps — report, human judges:
    for gid in missing:
        code = next(c for c, g in GAME_MAP.items() if g == gid)
        print(f"    missing: {gid} ({code})")

    print("== integrity ==")
    dup = ch.command("""
        SELECT count() - uniqExact(t_trade_receipt_ns, market_ticker, trade_price,
                                   trade_size_cc, taker_side, game_id)
        FROM enriched_trades""")
    check("no duplicate rows", dup == 0, f"{dup} dups")

    neg = ch.command(
        "SELECT countIf(r_info_delay_ms < 0) FROM enriched_trades WHERE r_info_delay_ms IS NOT NULL"
    )
    check("info delay never negative", neg == 0, f"{neg} negative")

    null_state = ch.command("SELECT countIf(r_score_diff IS NULL) FROM enriched_trades")
    pct = 100 * null_state / rows
    print(f"  trades with no game state yet (pregame etc.): {null_state:,} ({pct:.1f}%)")

    print("== settlement reality check (strongest) ==")
    # For each game: at the final buzzer, the model prob for a KXNBAGAME
    # ticker must be exactly 1.0 if the ticker's YES team won, 0.0 if it
    # lost. Winner is derived from r_score_diff (home diff) + the game map's
    # home team (last 3 chars of the event code); the ticker suffix names
    # the YES team. Fully independent of the join's own yes_is_home logic.
    code_by_gid = {gid: code for code, gid in GAME_MAP.items()}
    rows_bz = ch.query("""
        SELECT game_id, market_ticker, r_score_diff, r_model_prob
        FROM enriched_trades
        WHERE series = 'KXNBAGAME'
          AND r_period >= 4 AND r_seconds_remaining = 0
          AND r_model_prob IS NOT NULL AND r_score_diff != 0
        GROUP BY game_id, market_ticker, r_score_diff, r_model_prob
    """).result_rows
    wrong = []
    for gid, ticker, diff, prob in rows_bz:
        code = code_by_gid[gid]
        home, away = code[-3:], code[-6:-3]
        winner = home if diff > 0 else away
        yes_team = ticker.rsplit("-", 1)[1]
        expected = 1.0 if yes_team == winner else 0.0
        if prob != expected:
            wrong.append((gid, ticker, diff, prob, expected))
    check(
        "post-buzzer model prob is 1.0 for winner / 0.0 for loser in every game",
        not wrong,
        f"{len(wrong)} (ticker, prob) wrong, e.g. {wrong[:2]}" if wrong else "",
    )

    games_with_buzzer = ch.command("""
        SELECT uniqExact(game_id) FROM enriched_trades
        WHERE r_seconds_remaining = 0 AND r_period >= 4""")
    print(f"  games with post-buzzer trades: {games_with_buzzer}")

    print("== information delay (full dataset) ==")
    r = ch.query("""
        SELECT round(avg(r_info_delay_ms)/1000, 1),
               round(quantile(0.5)(r_info_delay_ms)/1000, 1),
               round(quantile(0.95)(r_info_delay_ms)/1000, 1)
        FROM enriched_trades WHERE r_info_delay_ms IS NOT NULL""").result_rows[0]
    print(f"  avg {r[0]}s | p50 {r[1]}s | p95 {r[2]}s  (golden game was avg 35s / p95 159s)")

    print()
    if failures:
        sys.exit(f"FAILURES: {failures}")
    print("backfill verification: all checks passed (review missing-games list above)")


if __name__ == "__main__":
    main()

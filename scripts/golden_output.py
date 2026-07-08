"""Freeze / verify the golden enriched output (DESIGN.md Testing Strategy).

The golden game's enriched output is the parity anchor: every pipeline change
must reproduce it exactly. This script:

  1. consumes all of enriched.trades from Kafka
  2. runs anchor assertions against hand-verified game facts
     (NYK 113-102 ATL; trade coverage 00:15-00:51 UTC = late Q4 + endgame)
  3. --freeze  : writes tests/fixtures/golden/enriched_0042500121.jsonl.gz
     --verify  : compares current output to the frozen fixture, byte-for-byte

Records are sorted deterministically (receipt ns, ticker, full payload) before
freezing/comparing — Kafka partition consumption order is not deterministic,
the join output content is.

Usage:
    python scripts/golden_output.py --freeze
    python scripts/golden_output.py --verify
"""

import argparse
import gzip
import json
import sys
from pathlib import Path

from confluent_kafka import Consumer, TopicPartition

FIXTURE = Path("tests/fixtures/golden/enriched_0042500121.jsonl.gz")
TOPIC = "enriched.trades"
EXPECTED_COUNT = 20_376
GAME_ID = "0042500121"
FINAL_DIFF = 11  # NYK 113 - ATL 102


def consume_all(bootstrap: str) -> list[dict]:
    c = Consumer(
        {"bootstrap.servers": bootstrap, "group.id": "golden-verify", "auto.offset.reset": "earliest"}
    )
    meta = c.list_topics(TOPIC, timeout=5).topics[TOPIC]
    parts = [TopicPartition(TOPIC, p, 0) for p in meta.partitions]
    targets = {p: c.get_watermark_offsets(TopicPartition(TOPIC, p), timeout=5)[1] for p in meta.partitions}
    c.assign(parts)

    records: list[dict] = []
    done: set[int] = {p for p, hi in targets.items() if hi == 0}
    while len(done) < len(targets):
        msg = c.poll(10)
        if msg is None:
            raise RuntimeError(f"timed out; got {len(records)} records, done={done}")
        records.append(json.loads(msg.value()))
        if msg.offset() + 1 >= targets[msg.partition()]:
            done.add(msg.partition())
    c.close()
    return records


def sort_key(r: dict) -> tuple:
    return (r["t_trade_receipt_ns"], r["market_ticker"], json.dumps(r, sort_keys=True))


def check_anchors(records: list[dict]) -> None:
    failures: list[str] = []

    def check(name: str, cond: bool) -> None:
        print(f"  {'PASS' if cond else 'FAIL'}  {name}")
        if not cond:
            failures.append(name)

    check(f"record count == {EXPECTED_COUNT}", len(records) == EXPECTED_COUNT)
    check("all records belong to golden game", all(r["game_id"] == GAME_ID for r in records))
    check(
        "every record has game state attached (trades cover late game only)",
        all(r["r_score_diff"] is not None for r in records),
    )
    check(
        "info delay non-negative wherever present",
        all(r["r_info_delay_ms"] >= 0 for r in records if r["r_info_delay_ms"] is not None),
    )

    # Post-final-buzzer trades must see the hand-verified final state
    post_final = [r for r in records if r["r_seconds_remaining"] == 0.0 and r["r_period"] == 4]
    check("trades exist after final buzzer", len(post_final) > 0)
    check(
        f"all post-buzzer trades see final score diff {FINAL_DIFF} (NYK 113-102)",
        all(r["r_score_diff"] == FINAL_DIFF for r in post_final),
    )

    # Game-winner markets: model prob present and side-consistent
    game_trades = [r for r in records if r["series"] == "KXNBAGAME"]
    check("KXNBAGAME trades have model prob", all(r["r_model_prob"] is not None for r in game_trades))
    nyk_end = [
        r
        for r in game_trades
        if r["market_ticker"].endswith("-NYK") and r["r_seconds_remaining"] == 0.0
    ]
    check(
        "NYK (winner, diff=+11) model prob = 1.0 at buzzer",
        all(r["r_model_prob"] == 1.0 for r in nyk_end) and len(nyk_end) > 0,
    )

    non_game = [r for r in records if r["series"] != "KXNBAGAME"]
    check(
        "non-GAME series have no model prob (not applicable)",
        all(r["r_model_prob"] is None for r in non_game),
    )

    if failures:
        sys.exit(f"\nANCHOR FAILURES: {failures}")


def main() -> None:
    parser = argparse.ArgumentParser()
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--freeze", action="store_true")
    mode.add_argument("--verify", action="store_true")
    parser.add_argument("--bootstrap-servers", default="localhost:9092")
    args = parser.parse_args()

    records = sorted(consume_all(args.bootstrap_servers), key=sort_key)
    print(f"consumed {len(records):,} records from {TOPIC}\n\nanchor checks:")
    check_anchors(records)

    lines = [json.dumps(r, sort_keys=True) for r in records]
    if args.freeze:
        with gzip.open(FIXTURE, "wt") as f:
            f.write("\n".join(lines) + "\n")
        print(f"\nfroze {len(lines):,} records -> {FIXTURE}")
    else:
        with gzip.open(FIXTURE, "rt") as f:
            frozen = f.read().splitlines()
        if lines == frozen:
            print(f"\nPARITY OK: output matches frozen fixture ({len(lines):,} records)")
        else:
            diff_count = sum(1 for a, b in zip(lines, frozen) if a != b) + abs(len(lines) - len(frozen))
            sys.exit(f"\nPARITY BROKEN: {diff_count} differing/missing lines vs fixture")


if __name__ == "__main__":
    main()

"""Paper trading stats for the MM strategy.

Usage:
    python -m scripts.mm_stats              # full summary
    python -m scripts.mm_stats pnl          # round-trip P&L only
    python -m scripts.mm_stats fills        # fill details
    python -m scripts.mm_stats quotes       # quoting activity
    python -m scripts.mm_stats positions    # open inventory
    python -m scripts.mm_stats status       # is the strategy running?
"""

from __future__ import annotations

import io
import sys
from datetime import datetime, timezone

import boto3
import pandas as pd

BUCKET = "prediction-markets-data"


def _load_silver(event_type: str, date: str | None = None) -> pd.DataFrame:
    s3 = boto3.client("s3")
    prefix = f"silver/kalshi_ws/{event_type}/"
    if date:
        prefix += f"date={date}/"
    paginator = s3.get_paginator("list_objects_v2")
    keys = [
        o["Key"]
        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix)
        for o in page.get("Contents", [])
    ]
    if not keys:
        return pd.DataFrame()
    return pd.concat(
        [
            pd.read_parquet(
                io.BytesIO(
                    s3.get_object(Bucket=BUCKET, Key=k)["Body"].read()
                )
            )
            for k in keys
        ],
        ignore_index=True,
    )


def cmd_pnl() -> None:
    fills = _load_silver("MMFillEvent")
    if fills.empty:
        print("No fills yet.")
        return

    pnl_rows = []
    for ticker, g in fills.groupby("market_ticker"):
        buys = g[g["side"] == "buy"].sort_values("t_receipt")
        sells = g[g["side"] == "sell"].sort_values("t_receipt")
        pairs = min(len(buys), len(sells))
        if pairs == 0:
            continue
        for i in range(pairs):
            b, s = buys.iloc[i], sells.iloc[i]
            pnl = s["price"] - b["price"] - b["maker_fee"] - s["maker_fee"]
            pnl_rows.append({
                "ticker": ticker,
                "buy": b["price"],
                "sell": s["price"],
                "fees": b["maker_fee"] + s["maker_fee"],
                "pnl_cents": pnl,
            })

    if not pnl_rows:
        print(f"No round-trips yet ({len(fills)} fills, need buy+sell on same ticker)")
        return

    rt = pd.DataFrame(pnl_rows)
    total = rt["pnl_cents"].sum()
    print(f"Round-trips:  {len(rt)}")
    print(f"P&L:          {total:+.0f}c (${total/100:+.2f})")
    print(f"Win rate:     {(rt['pnl_cents'] > 0).mean():.0%}")
    print(f"Avg per RT:   {rt['pnl_cents'].mean():+.1f}c")
    print(f"Total fees:   {rt['fees'].sum():.0f}c")
    print()

    by_ticker = rt.groupby("ticker")["pnl_cents"].sum().sort_values()
    if len(by_ticker) >= 2:
        print("Best:")
        for t, p in by_ticker.tail(3).items():
            print(f"  {p:+4.0f}c  {t}")
        print("Worst:")
        for t, p in by_ticker.head(3).items():
            print(f"  {p:+4.0f}c  {t}")


def cmd_fills() -> None:
    fills = _load_silver("MMFillEvent")
    if fills.empty:
        print("No fills yet.")
        return

    print(f"Total fills: {len(fills)}")
    print(f"  Buys:    {(fills['side'] == 'buy').sum()}")
    print(f"  Sells:   {(fills['side'] == 'sell').sum()}")
    print(f"  Tickers: {fills['market_ticker'].nunique()}")
    print()

    # By date
    fills["date"] = pd.to_datetime(fills["t_receipt"], unit="s").dt.date
    by_date = fills.groupby("date").size()
    print("Fills by date:")
    for date, count in by_date.items():
        print(f"  {date}: {count}")
    print()

    print("Recent fills:")
    recent = fills.sort_values("t_receipt").tail(10)
    for _, f in recent.iterrows():
        ts = datetime.fromtimestamp(f["t_receipt"], tz=timezone.utc).strftime("%H:%M:%S")
        print(f"  {ts} {f['side']:4s} {f['market_ticker']} @{f['price']}c pos={f['position_after']}")


def cmd_quotes() -> None:
    quotes = _load_silver("MMQuoteEvent")
    if quotes.empty:
        print("No quote events.")
        return

    active = quotes[quotes["bid_price"].notna()]
    inactive = quotes[quotes["bid_price"].isna() & quotes["ask_price"].isna()]

    print(f"Total quote events: {len(quotes)}")
    print(f"  Actively quoting: {len(active)}")
    print(f"  Not quoting:      {len(inactive)}")
    print(f"  Unique tickers:   {active['market_ticker'].nunique()}")
    print()

    if len(inactive) > 0:
        reasons = inactive["reason_no_bid"].value_counts().to_dict()
        print(f"Reasons not quoting: {reasons}")
        print()

    if len(active) > 0:
        print(f"Spread stats (when quoting):")
        print(f"  Median: {active['spread'].median():.0f}c")
        print(f"  Mean:   {active['spread'].mean():.1f}c")
        print(f"  Min:    {active['spread'].min():.0f}c")
        print(f"  Max:    {active['spread'].max():.0f}c")


def cmd_positions() -> None:
    fills = _load_silver("MMFillEvent")
    if fills.empty:
        print("No fills — no positions.")
        return

    open_pos = fills.groupby("market_ticker")["position_after"].last()
    open_pos = open_pos[open_pos != 0]

    if open_pos.empty:
        print("No open positions — all flat.")
        return

    print(f"Open positions: {len(open_pos)} tickers")
    print(f"  Net contracts: {open_pos.sum()}")
    print(f"  Abs exposure:  {open_pos.abs().sum()} contracts")
    print()
    for ticker, pos in open_pos.sort_values().items():
        print(f"  {pos:+3d}  {ticker}")


def cmd_status() -> None:
    import subprocess
    result = subprocess.run(
        ["systemctl", "is-active", "kalshi-live.service"],
        capture_output=True, text=True,
    )
    svc_status = result.stdout.strip()

    # Check for MM_ENABLED in override
    result2 = subprocess.run(
        ["systemctl", "cat", "kalshi-live.service"],
        capture_output=True, text=True,
    )
    mm_enabled = "MM_ENABLED=1" in result2.stdout

    print(f"Service:     {svc_status}")
    print(f"MM enabled:  {'yes' if mm_enabled else 'no'}")

    # Latest silver files
    s3 = boto3.client("s3")
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    for event_type in ["MMQuoteEvent", "MMFillEvent", "TradeEvent", "OrderBookUpdate"]:
        prefix = f"silver/kalshi_ws/{event_type}/date={today}/"
        paginator = s3.get_paginator("list_objects_v2")
        keys = [
            o for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix)
            for o in page.get("Contents", [])
        ]
        if keys:
            latest = max(keys, key=lambda o: o["LastModified"])
            age_s = (datetime.now(timezone.utc) - latest["LastModified"]).total_seconds()
            age_m = int(age_s // 60)
            print(f"  {event_type}: {len(keys)} files today, latest {age_m}m ago")
        else:
            print(f"  {event_type}: no files today")


def cmd_summary() -> None:
    print("=== STATUS ===")
    cmd_status()
    print()
    print("=== QUOTES ===")
    cmd_quotes()
    print()
    print("=== FILLS ===")
    cmd_fills()
    print()
    print("=== P&L ===")
    cmd_pnl()
    print()
    print("=== POSITIONS ===")
    cmd_positions()


COMMANDS = {
    "pnl": cmd_pnl,
    "fills": cmd_fills,
    "quotes": cmd_quotes,
    "positions": cmd_positions,
    "status": cmd_status,
}


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else None
    if cmd in ("-h", "--help"):
        print(__doc__)
        return
    if cmd and cmd in COMMANDS:
        COMMANDS[cmd]()
    else:
        cmd_summary()


if __name__ == "__main__":
    main()

"""Paper trading stats for the MM strategy.

Usage:
    python -m scripts.mm_stats              # full summary
    python -m scripts.mm_stats pnl          # round-trip P&L only
    python -m scripts.mm_stats fills        # fill details
    python -m scripts.mm_stats quotes       # quoting activity
    python -m scripts.mm_stats positions    # open inventory
    python -m scripts.mm_stats exposure     # unrealized P&L + combined totals
    python -m scripts.mm_stats status       # is the strategy running?
"""

from __future__ import annotations

import io
import sys
from datetime import datetime, timezone

import boto3
import pandas as pd

from app.core.config import SILVER_VERSION

BUCKET = "prediction-markets-data"


def _load_silver(event_type: str, date: str | None = None) -> pd.DataFrame:
    s3 = boto3.client("s3")
    prefix = f"silver/kalshi_ws/{event_type}/"
    if date:
        prefix += f"date={date}/v={SILVER_VERSION}/"
    paginator = s3.get_paginator("list_objects_v2")
    version_segment = f"/v={SILVER_VERSION}/"
    keys = [
        o["Key"]
        for page in paginator.paginate(Bucket=BUCKET, Prefix=prefix)
        for o in page.get("Contents", [])
        if version_segment in o["Key"]
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


def _true_position(g: pd.DataFrame) -> int:
    """Compute true economic position from fill deltas, ignoring position resets."""
    pos = 0
    for _, r in g.iterrows():
        if r["side"] == "buy":
            pos += r["fill_size"]
        else:
            pos -= r["fill_size"]
    return pos


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

    open_pos = fills.groupby("market_ticker").apply(
        _true_position, include_groups=False,
    )
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


def _lookup_markets(tickers: list[str]) -> dict[str, dict]:
    """Look up settlement result and current yes_bid/yes_ask for each ticker.

    Returns {ticker: {"result": "yes"|"no"|None, "yes_bid": int|None, "yes_ask": int|None}}.
    """
    import requests

    results: dict[str, dict] = {}
    for i, ticker in enumerate(tickers, 1):
        if i % 10 == 0 or i == len(tickers):
            print(f"  Looking up markets... {i}/{len(tickers)}", end="\r")
        try:
            resp = requests.get(
                f"https://api.elections.kalshi.com/trade-api/v2/markets/{ticker}",
                timeout=10,
            )
            resp.raise_for_status()
            m = resp.json().get("market", {})
            result = m.get("result")
            yes_bid_d = m.get("yes_bid_dollars")
            yes_ask_d = m.get("yes_ask_dollars")
            results[ticker] = {
                "result": result if result in ("yes", "no") else None,
                "yes_bid": int(round(float(yes_bid_d) * 100)) if yes_bid_d is not None else None,
                "yes_ask": int(round(float(yes_ask_d) * 100)) if yes_ask_d is not None else None,
            }
        except Exception:
            results[ticker] = {"result": None, "yes_bid": None, "yes_ask": None}
    if len(tickers) >= 10:
        print()
    return results


def cmd_exposure() -> None:
    """Unrealized P&L for open positions, using actual settlement results from Kalshi."""
    fills = _load_silver("MMFillEvent")
    if fills.empty:
        print("No fills yet.")
        return

    # --- realized P&L (same logic as cmd_pnl) ---
    realized_total = 0
    realized_fees = 0
    rt_count = 0
    for _, g in fills.groupby("market_ticker"):
        buys = g[g["side"] == "buy"].sort_values("t_receipt")
        sells = g[g["side"] == "sell"].sort_values("t_receipt")
        pairs = min(len(buys), len(sells))
        for i in range(pairs):
            b, s = buys.iloc[i], sells.iloc[i]
            realized_total += s["price"] - b["price"] - b["maker_fee"] - s["maker_fee"]
            realized_fees += b["maker_fee"] + s["maker_fee"]
            rt_count += 1

    # --- find unpaired fills per ticker ---
    # Use true economic position (sum of deltas) to handle strategy restarts
    open_tickers = []
    unpaired_by_ticker: dict[str, dict] = {}
    for ticker, g in fills.groupby("market_ticker"):
        g = g.sort_values("t_receipt")
        pos = _true_position(g)
        if pos == 0:
            continue

        buys = g[g["side"] == "buy"].sort_values("t_receipt")
        sells = g[g["side"] == "sell"].sort_values("t_receipt")
        pairs = min(len(buys), len(sells))

        if pos > 0:
            # Take the LAST abs(pos) unpaired buys (most recent)
            unpaired = buys.iloc[pairs:]
            if len(unpaired) > abs(pos):
                unpaired = unpaired.iloc[-abs(pos):]
        else:
            unpaired = sells.iloc[pairs:]
            if len(unpaired) > abs(pos):
                unpaired = unpaired.iloc[-abs(pos):]

        open_tickers.append(ticker)
        unpaired_by_ticker[ticker] = {
            "pos": int(pos),
            "avg_entry": unpaired["price"].mean(),
            "fees": unpaired["maker_fee"].sum(),
            "unpaired": unpaired,
        }

    if not open_tickers:
        print("No open positions.")
        return

    # --- look up settlements + current prices ---
    print(f"Looking up {len(open_tickers)} markets from Kalshi API...")
    market_data = _lookup_markets(open_tickers)

    rows = []
    for ticker in open_tickers:
        info = unpaired_by_ticker[ticker]
        unpaired = info["unpaired"]
        pos = info["pos"]
        mkt = market_data.get(ticker, {})
        settlement = mkt.get("result")
        yes_bid = mkt.get("yes_bid")
        yes_ask = mkt.get("yes_ask")
        mid = ((yes_bid + yes_ask) // 2) if yes_bid is not None and yes_ask is not None else None

        if settlement is not None:
            if pos > 0:
                if settlement == "yes":
                    pnl = ((100 - unpaired["price"]) - unpaired["maker_fee"]).sum()
                else:
                    pnl = -(unpaired["price"] + unpaired["maker_fee"]).sum()
            else:
                if settlement == "no":
                    pnl = (unpaired["price"] - unpaired["maker_fee"]).sum()
                else:
                    pnl = -(100 - unpaired["price"] + unpaired["maker_fee"]).sum()
            status = f"{'YES' if settlement == 'yes' else 'NO':>3s}"
        else:
            # Mark-to-market: unrealized P&L based on current mid
            if mid is not None:
                if pos > 0:
                    pnl = ((mid - unpaired["price"]) - unpaired["maker_fee"]).sum()
                else:
                    pnl = ((unpaired["price"] - mid) - unpaired["maker_fee"]).sum()
            else:
                pnl = None
            status = "  ?"

        rows.append({
            "ticker": ticker,
            "pos": pos,
            "avg_entry": info["avg_entry"],
            "mid": mid,
            "fees": info["fees"],
            "settlement": settlement,
            "status": status,
            "pnl": pnl,
        })

    exp = pd.DataFrame(rows).sort_values("pnl", na_position="last")

    settled = exp[exp["settlement"].notna()]
    unsettled = exp[exp["settlement"].isna()]
    n_short = (exp["pos"] < 0).sum()
    n_long = (exp["pos"] > 0).sum()

    print(f"\nOpen positions: {len(exp)} tickers ({n_short} short YES, {n_long} long YES)")
    print(f"  Settled:    {len(settled)}")
    print(f"  Unsettled:  {len(unsettled)}")
    print()

    print(f"{'Ticker':<55s}  {'Pos':>4s}  {'Entry':>5s}  {'Mid':>5s}  {'Result':>6s}  {'P&L':>7s}")
    print("-" * 92)
    for _, r in exp.iterrows():
        pnl_str = f"{r['pnl']:>+7.0f}c" if pd.notna(r["pnl"]) else "      ?"
        mid_str = f"{r['mid']:>5.0f}c" if pd.notna(r.get("mid")) and r["mid"] is not None else "    ?"
        print(f"{r['ticker']:<55s}  {r['pos']:>+4d}  {r['avg_entry']:>5.0f}c  "
              f"{mid_str}  {r['status']:>6s}  {pnl_str}")

    # --- summary ---
    print()
    if len(settled) > 0:
        settled_pnl = settled["pnl"].sum()
        settled_wins = (settled["pnl"] > 0).sum()
        settled_losses = (settled["pnl"] <= 0).sum()
        print(f"Settled open P&L:      {settled_pnl:+.0f}c (${settled_pnl/100:+.2f})  "
              f"[{settled_wins}W / {settled_losses}L]")

    if len(unsettled) > 0:
        mtm = unsettled[unsettled["pnl"].notna()]
        if len(mtm) > 0:
            mtm_pnl = mtm["pnl"].sum()
            mtm_wins = (mtm["pnl"] > 0).sum()
            mtm_losses = (mtm["pnl"] <= 0).sum()
            print(f"Unsettled mark-to-mkt: {mtm_pnl:+.0f}c (${mtm_pnl/100:+.2f})  "
                  f"[{mtm_wins}W / {mtm_losses}L]  ({len(unsettled)} positions)")
        else:
            print(f"Unsettled:             {len(unsettled)} positions (no mid available)")

    print()
    print("--- TOTAL P&L ---")
    print(f"Realized (round-trips):  {realized_total:+.0f}c (${realized_total/100:+.2f})  "
          f"[{rt_count} trades]")
    if len(settled) > 0:
        settled_pnl_val = settled_pnl
        print(f"+ Settled open pos:      {settled_pnl_val:+.0f}c (${settled_pnl_val/100:+.2f})")
    else:
        settled_pnl_val = 0
    unsettled_mtm = unsettled[unsettled["pnl"].notna()]
    if len(unsettled_mtm) > 0:
        mtm_val = unsettled_mtm["pnl"].sum()
        print(f"+ Unsettled (mark-to-mkt): {mtm_val:+.0f}c (${mtm_val/100:+.2f})")
        total = realized_total + settled_pnl_val + mtm_val
    else:
        total = realized_total + settled_pnl_val
    print(f"= Est. total P&L:       {total:+.0f}c (${total/100:+.2f})")


def cmd_status() -> None:
    import shutil
    import subprocess

    if shutil.which("systemctl"):
        result = subprocess.run(
            ["systemctl", "is-active", "kalshi-live.service"],
            capture_output=True, text=True,
        )
        svc_status = result.stdout.strip()
        result2 = subprocess.run(
            ["systemctl", "cat", "kalshi-live.service"],
            capture_output=True, text=True,
        )
        mm_enabled = "MM_ENABLED=1" in result2.stdout
        print(f"Service:     {svc_status}")
        print(f"MM enabled:  {'yes' if mm_enabled else 'no'}")
    else:
        print("Service:     (not on EC2, skipping systemd check)")

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
    print()
    print("=== EXPOSURE ===")
    cmd_exposure()


COMMANDS = {
    "pnl": cmd_pnl,
    "fills": cmd_fills,
    "quotes": cmd_quotes,
    "positions": cmd_positions,
    "exposure": cmd_exposure,
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

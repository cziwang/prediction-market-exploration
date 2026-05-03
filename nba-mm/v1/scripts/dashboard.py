"""MM Trading Dashboard — real-time Kalshi KXNBAPTS monitoring.

Run: streamlit run scripts/dashboard.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure repo root is on sys.path so `app.*` and `scripts.*` imports work
_repo = str(Path(__file__).resolve().parent.parent)
if _repo not in sys.path:
    sys.path.insert(0, _repo)

import time
from datetime import date, datetime, timedelta, timezone

import streamlit as st
from streamlit_autorefresh import st_autorefresh

from scripts.dashboard.ws_client import DashboardState, KalshiWSClient
from scripts.dashboard.trading import (
    cancel_all_orders, cancel_order, flatten_position, place_order,
    fetch_positions, fetch_settlements,
)

st.set_page_config(page_title="MM Dashboard", layout="wide")


# ---------------------------------------------------------------------------
# Session state: WS client singleton
# ---------------------------------------------------------------------------

def _get_state() -> DashboardState:
    if "dash_state" not in st.session_state:
        state = DashboardState()
        # Seed positions and orders from REST so they're available on first render
        _seed_from_rest(state)
        st.session_state.dash_state = state
    return st.session_state.dash_state


def _seed_from_rest(state: DashboardState) -> None:
    """Fetch positions and resting orders via REST to populate state immediately."""
    from scripts.dashboard.trading import fetch_positions, fetch_resting_orders
    from app.transforms.kalshi_ws import _dollars_to_cents

    try:
        rest_positions = fetch_positions()
        rest_orders = fetch_resting_orders()
        with state.lock:
            for mp in rest_positions:
                ticker = mp.get("ticker", "")
                pos = int(round(float(mp.get("position_fp", "0"))))
                if ticker and pos != 0:
                    state.positions[ticker] = {
                        "ticker": ticker,
                        "position": pos,
                        "cost_dollars": float(mp.get("market_exposure_dollars", "0")),
                        "realized_pnl": float(mp.get("realized_pnl_dollars", "0")),
                        "fees_paid": float(mp.get("fees_paid_dollars", "0")),
                        "volume": 0,
                    }
            for o in rest_orders:
                order_id = o.get("order_id", "")
                if order_id:
                    state.resting_orders[order_id] = {
                        "order_id": order_id,
                        "ticker": o.get("ticker", ""),
                        "side": o.get("side", ""),
                        "price": _dollars_to_cents(str(o.get("yes_price_dollars", "0"))),
                        "initial_size": int(round(float(o.get("initial_count_fp", "0")))),
                        "remaining": int(round(float(o.get("remaining_count_fp", "0")))),
                        "fill_count": int(round(float(o.get("fill_count_fp", "0")))),
                        "status": o.get("status", ""),
                        "maker_fees": float(o.get("maker_fees_dollars", "0")),
                        "taker_fees": float(o.get("taker_fees_dollars", "0")),
                        "updated_ms": o.get("last_updated_ts_ms", 0),
                    }
    except Exception as e:
        state.error = f"REST seed failed: {e}"


def _get_client() -> KalshiWSClient:
    if "ws_client" not in st.session_state:
        state = _get_state()
        client = KalshiWSClient(state)
        client.start()
        st.session_state.ws_client = client
    return st.session_state.ws_client


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _short_ticker(ticker: str) -> str:
    """KXNBAPTS-26APR25OKCPHX-PHXDBROOKS3-25 → PHXDBROOKS3-25"""
    parts = ticker.split("-")
    if len(parts) >= 4:
        return f"{parts[2]}-{parts[3]}"
    return ticker


def _compute_positions_df(state: DashboardState) -> list[dict]:
    """Build positions table with live book data."""
    rows = []
    with state.lock:
        for ticker, pos_data in state.positions.items():
            pos = pos_data["position"]
            if pos == 0:
                continue
            book = state.books.get(ticker)
            bid = book.best_bid if book else None
            ask = book.best_ask if book else None
            mid = book.mid if book else None
            spread = (ask - bid) if bid is not None and ask is not None else None

            # Compute entry price from cost basis
            cost = pos_data["cost_dollars"]
            entry_cents = int(round(abs(cost) / abs(pos) * 100)) if pos != 0 and cost != 0 else 0

            # MTM P&L
            if mid is not None and entry_cents > 0:
                if pos > 0:
                    mtm = (mid - entry_cents) * pos
                else:
                    mtm = (entry_cents - mid) * abs(pos)
            else:
                mtm = 0

            market_st = state.market_status.get(ticker, "active")

            rows.append({
                "Ticker": _short_ticker(ticker),
                "Full Ticker": ticker,
                "Pos": pos,
                "Entry": entry_cents,
                "Bid": bid,
                "Ask": ask,
                "Mid": mid,
                "Spread": spread,
                "MTM": mtm,
                "Realized": int(round(pos_data["realized_pnl"] * 100)),
                "Fees": int(round(pos_data["fees_paid"] * 100)),
                "Status": market_st,
            })
    rows.sort(key=lambda r: r["MTM"])
    return rows


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@st.cache_data(ttl=300)
def _fetch_pnl_chart_data() -> list[dict]:
    """Fetch fills and settlements and compute cumulative P&L events.

    Cached for 5 minutes to avoid hammering the API on every 1s refresh.
    """
    from scripts.dashboard.trading import fetch_fills, fetch_settlements
    fills = fetch_fills()
    settlements = fetch_settlements()

    events: list[dict] = []
    for f in fills:
        ts = f.get("created_time", "")
        if not ts:
            continue
        count = float(f.get("count_fp", "0"))
        fee = float(f.get("fee_cost", "0"))
        action = f.get("action", "")
        side = f.get("side", "yes")
        if side == "yes":
            price = float(f.get("yes_price_dollars", "0"))
        else:
            price = float(f.get("no_price_dollars", "0"))
        if action == "buy":
            cash = -(price * count) - fee
        else:
            cash = (price * count) - fee
        events.append({"time": ts, "cash": cash})

    for s in settlements:
        result = s["market_result"]
        yes_count = float(s["yes_count_fp"])
        no_count = float(s["no_count_fp"])
        payout = yes_count if result == "yes" else no_count
        ts = s.get("settled_time") or s.get("last_updated_ts") or ""
        if ts and payout > 0:
            if isinstance(ts, (int, float)):
                ts = datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
            events.append({"time": ts, "cash": payout})

    return events


def _render_pnl_chart() -> None:
    """Render cumulative P&L line chart on the main dashboard."""
    import pandas as pd

    events = _fetch_pnl_chart_data()
    if not events:
        return

    st.subheader("Portfolio Performance")
    df = pd.DataFrame(events)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.sort_values("time")
    df["Cumulative P&L ($)"] = df["cash"].cumsum()
    df = df.set_index("time")
    st.line_chart(df["Cumulative P&L ($)"])


def main():
    # Silent auto-refresh every 1s — no spinner, no Stop button
    st_autorefresh(interval=1000, limit=None, key="refresh")

    state = _get_state()
    client = _get_client()

    # --- Connection status + overview bar ---
    with state.lock:
        connected = state.connected
        last_msg = state.last_msg_time
        error = state.error
        n_tickers = len([t for t, p in state.positions.items() if p["position"] != 0])
        total_abs = sum(abs(p["position"]) for p in state.positions.values())
        total_net = sum(p["position"] for p in state.positions.values())
        total_realized = sum(p["realized_pnl"] for p in state.positions.values())
        total_fees = sum(p["fees_paid"] for p in state.positions.values())
        n_fills = len(state.fills)

    # Compute total MTM from positions
    pos_rows = _compute_positions_df(state)
    total_mtm = sum(r["MTM"] for r in pos_rows)

    # Header
    age_s = time.time() - last_msg if last_msg > 0 else float("inf")
    if connected and age_s < 10:
        status_str = ":green[LIVE]"
    elif connected:
        status_str = f":orange[STALE ({age_s:.0f}s)]"
    else:
        status_str = ":red[DISCONNECTED]"

    net_pnl = total_realized - total_fees + total_mtm / 100

    cols = st.columns([1, 2, 3, 2, 1])
    cols[0].markdown(f"### {status_str}")
    cols[1].metric("Positions", f"{n_tickers} tickers, {total_abs} abs, {total_net:+d} net")
    cols[2].metric("Net P&L", f"${net_pnl:+.2f}",
                    delta=f"R: ${total_realized:+.2f}  F: -${total_fees:.2f}  M: ${total_mtm/100:+.2f}")
    cols[3].metric("Fills", f"{n_fills} today")

    # Kill all button
    if cols[4].button("KILL ALL", type="primary", width="stretch"):
        try:
            cancel_all_orders()
            st.success("All orders cancelled")
        except Exception as e:
            st.error(f"Cancel failed: {e}")

    if error:
        st.warning(f"WS error: {error}")

    st.divider()

    # --- Main content: two columns ---
    left, right = st.columns([3, 2])

    # --- Positions table ---
    with left:
        st.subheader("Positions")
        if pos_rows:
            import pandas as pd
            df = pd.DataFrame(pos_rows)
            display_df = df[["Ticker", "Pos", "Entry", "Bid", "Ask", "Spread", "MTM", "Status"]].copy()
            display_df["Entry"] = display_df["Entry"].apply(lambda x: f"{x}c" if x else "?")
            display_df["Bid"] = display_df["Bid"].apply(lambda x: f"{x}c" if x is not None else "?")
            display_df["Ask"] = display_df["Ask"].apply(lambda x: f"{x}c" if x is not None else "?")
            display_df["Spread"] = display_df["Spread"].apply(lambda x: f"{x}c" if x is not None else "?")
            display_df["MTM"] = display_df["MTM"].apply(lambda x: f"{x:+d}c")

            st.dataframe(display_df, width="stretch", hide_index=True)

            # Flatten buttons
            st.caption("Flatten a position:")
            flatten_cols = st.columns(3)
            ticker_options = [r["Full Ticker"] for r in pos_rows if r["Pos"] != 0]
            if ticker_options:
                selected = flatten_cols[0].selectbox("Ticker", ticker_options, label_visibility="collapsed")
                if flatten_cols[1].button("Flatten"):
                    row = next(r for r in pos_rows if r["Full Ticker"] == selected)
                    bid = row["Bid"] or 1
                    ask = row["Ask"] or 99
                    try:
                        flatten_position(selected, row["Pos"], bid, ask)
                        st.success(f"Flatten order sent for {_short_ticker(selected)}")
                    except Exception as e:
                        st.error(f"Flatten failed: {e}")
        else:
            st.info("No open positions")

    # --- Fills + trades feed ---
    with right:
        tab_fills, tab_trades, tab_orders = st.tabs(["Fills", "Trades", "Orders"])

        with tab_fills:
            with state.lock:
                fills = list(reversed(state.fills[-50:]))
            if fills:
                for f in fills:
                    ts = datetime.fromtimestamp(f["t_ms"] / 1000, tz=timezone.utc).strftime("%H:%M:%S")
                    side_icon = "+" if f["action"] == "buy" else "-"
                    color = "green" if f["action"] == "buy" else "red"
                    st.markdown(
                        f":{color}[{ts}  {f['action'].upper():4s}  "
                        f"{_short_ticker(f['ticker'])}  @{f['price']}c  "
                        f"x{f['size']}  fee={f['fee']:.4f}  "
                        f"pos→{f['post_position']}]"
                    )
            else:
                st.info("No fills yet")

        with tab_trades:
            with state.lock:
                trades = list(state.recent_trades)[:50]
            if trades:
                for t in trades:
                    ts = datetime.fromtimestamp(t["t"], tz=timezone.utc).strftime("%H:%M:%S")
                    side = "BUY" if t["side"] == "yes" else "SELL"
                    color = "green" if t["side"] == "yes" else "red"
                    st.markdown(
                        f":{color}[{ts}  {side:4s}  "
                        f"{_short_ticker(t['ticker'])}  @{t['price']}c  x{t['size']}]"
                    )
            else:
                st.info("No trades yet")

        with tab_orders:
            with state.lock:
                orders = list(state.resting_orders.values())
            if orders:
                for o in orders:
                    st.markdown(
                        f"{_short_ticker(o['ticker'])}  {o['side'].upper()}  "
                        f"@{o['price']}c  {o['remaining']}/{o['initial_size']}  "
                        f"*{o['status']}*"
                    )
                    if st.button(f"Cancel {o['order_id'][:8]}", key=o["order_id"]):
                        try:
                            cancel_order(o["order_id"])
                            st.success("Cancelled")
                        except Exception as e:
                            st.error(f"Cancel failed: {e}")
            else:
                st.info("No resting orders")

    # --- Portfolio performance chart ---
    st.divider()
    _render_pnl_chart()

    # --- Trade panel (bottom) ---
    st.divider()
    with st.expander("Place Order", expanded=False):
        with state.lock:
            all_tickers = sorted(
                t for t in state.subscribed_tickers
                if state.market_status.get(t, "active") == "active"
            )

        row1 = st.columns([4, 1])
        ticker = row1[0].selectbox("Ticker", all_tickers if all_tickers else ["(no active tickers)"])

        # Show live book for selected ticker
        with state.lock:
            book = state.books.get(ticker)
        if book:
            bid = book.best_bid
            ask = book.best_ask
            mid = book.mid
            spread = (ask - bid) if bid is not None and ask is not None else None
            row1[1].markdown(
                f"**Bid** {bid}c &nbsp; **Ask** {ask}c &nbsp; "
                f"**Mid** {mid}c &nbsp; **Spread** {spread}c"
                if bid is not None else "Book not loaded"
            )
        else:
            row1[1].markdown("*Book not loaded*")
            bid, ask = None, None

        row2 = st.columns([1, 1, 1, 1, 1])
        action = row2[0].selectbox("Action", ["buy", "sell"])
        order_type = row2[1].selectbox("Type", ["limit", "market"])

        if order_type == "market":
            # Market order: cross the spread
            if action == "buy" and ask is not None:
                price = ask
                row2[2].metric("Price", f"{price}c (ask)")
            elif action == "sell" and bid is not None:
                price = bid
                row2[2].metric("Price", f"{price}c (bid)")
            else:
                price = 50
                row2[2].warning("No book")
        else:
            price = row2[2].number_input("Price (cents)", min_value=1, max_value=99, value=50)

        size = row2[3].number_input("Size", min_value=1, max_value=10, value=1)
        if row2[4].button("Submit Order"):
            try:
                result = place_order(ticker, "yes", action, price, size)
                st.success(f"Order placed: {result}")
            except Exception as e:
                st.error(f"Order failed: {e}")

    # --- P&L Summary ---
    st.divider()
    with st.expander("P&L Summary", expanded=False):
        _render_pnl_summary(state)


def _render_pnl_summary(state: DashboardState) -> None:
    """P&L summary with timeframe selector, covering fills + open positions."""
    import pandas as pd

    # Timeframe selector
    tf_cols = st.columns([1, 1, 1, 1])
    timeframe = tf_cols[0].selectbox("Timeframe", ["Today", "Last 7 days", "Last 30 days", "All time", "Custom"])
    now = datetime.now(timezone.utc)

    if timeframe == "Custom":
        start_date = tf_cols[1].date_input("Start", value=now.date() - timedelta(days=7))
        end_date = tf_cols[2].date_input("End", value=now.date())
        min_ts = int(datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc).timestamp())
        max_ts = int(datetime.combine(end_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc).timestamp())
    elif timeframe == "Today":
        min_ts = int(datetime.combine(now.date(), datetime.min.time(), tzinfo=timezone.utc).timestamp())
        max_ts = None
    elif timeframe == "Last 7 days":
        min_ts = int((now - timedelta(days=7)).timestamp())
        max_ts = None
    elif timeframe == "Last 30 days":
        min_ts = int((now - timedelta(days=30)).timestamp())
        max_ts = None
    else:  # All time
        min_ts = None
        max_ts = None

    if tf_cols[3].button("Load P&L"):
        try:
            settlements = fetch_settlements(min_ts=min_ts, max_ts=max_ts)
            positions = fetch_positions()
            st.session_state.pnl_settlements = settlements
            st.session_state.pnl_positions = positions
            st.session_state.pnl_loaded = True
        except Exception as e:
            st.error(f"Failed to fetch P&L data: {e}")
            return

    if not st.session_state.get("pnl_loaded"):
        st.info("Click 'Load P&L' to fetch data")
        return

    settlements = st.session_state.pnl_settlements
    positions = st.session_state.pnl_positions

    # --- Settled markets P&L ---
    settled_rows = []
    total_settled_pnl = 0.0
    total_settled_fees = 0.0
    for s in settlements:
        result = s["market_result"]
        yes_count = float(s["yes_count_fp"])
        no_count = float(s["no_count_fp"])
        yes_cost = float(s["yes_total_cost_dollars"])
        no_cost = float(s["no_total_cost_dollars"])
        fee = float(s["fee_cost"])
        # Payout: winning side gets $1 per contract
        payout = yes_count if result == "yes" else no_count
        total_cost = yes_cost + no_cost
        pnl = payout - total_cost - fee

        total_settled_pnl += pnl
        total_settled_fees += fee

        settled_rows.append({
            "Ticker": _short_ticker(s["ticker"]),
            "Result": result.upper(),
            "YES": f"{int(yes_count)}x ${yes_cost:.2f}",
            "NO": f"{int(no_count)}x ${no_cost:.2f}",
            "Payout": f"${payout:.2f}",
            "Fees": f"${fee:.2f}",
            "P&L": f"${pnl:+.2f}",
        })
    settled_rows.sort(key=lambda r: float(r["P&L"].replace("$", "").replace("+", "")))

    # --- Open positions unrealized P&L ---
    open_rows = []
    total_open_pnl = 0.0
    for p in positions:
        ticker = p.get("ticker", "")
        pos = int(round(float(p.get("position_fp", "0"))))
        if pos == 0:
            continue
        cost = float(p.get("market_exposure_dollars", "0"))
        entry_cents = int(round(abs(cost) / abs(pos) * 100)) if pos != 0 else 0

        mid = None
        with state.lock:
            book = state.books.get(ticker)
        if book:
            mid = book.mid

        if mid is not None:
            if pos > 0:
                unrealized = (mid - entry_cents) * pos / 100
            else:
                unrealized = (entry_cents - mid) * abs(pos) / 100
        else:
            unrealized = 0.0

        total_open_pnl += unrealized
        open_rows.append({
            "Ticker": _short_ticker(ticker),
            "Pos": pos,
            "Entry": f"{entry_cents}c",
            "Mid": f"{mid}c" if mid is not None else "?",
            "Unrealized": f"${unrealized:+.2f}",
        })
    open_rows.sort(key=lambda r: float(r["Unrealized"].replace("$", "").replace("+", "")))

    # --- Summary metrics ---
    total_net = total_settled_pnl + total_open_pnl
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Net P&L", f"${total_net:+.2f}")
    m2.metric("Settled", f"${total_settled_pnl:+.2f}")
    m3.metric("Unrealized", f"${total_open_pnl:+.2f}")
    m4.metric("Fees", f"${total_settled_fees:.2f}")

    # --- Tables ---
    if settled_rows:
        st.caption(f"{len(settled_rows)} settled markets")
        df_settled = pd.DataFrame(settled_rows)
        st.dataframe(df_settled, width="stretch", hide_index=True)
    else:
        st.info("No settlements in this timeframe")

    if open_rows:
        st.caption(f"{len(open_rows)} open positions")
        df_open = pd.DataFrame(open_rows)
        st.dataframe(df_open, width="stretch", hide_index=True)


if __name__ == "__main__":
    main()

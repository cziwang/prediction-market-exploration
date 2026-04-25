"""MM Trading Dashboard — real-time Kalshi KXNBAPTS monitoring.

Run: streamlit run scripts/dashboard.py
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import streamlit as st

from scripts.dashboard.ws_client import DashboardState, KalshiWSClient
from scripts.dashboard.trading import cancel_all_orders, cancel_order, flatten_position, place_order

st.set_page_config(page_title="MM Dashboard", layout="wide")


# ---------------------------------------------------------------------------
# Session state: WS client singleton
# ---------------------------------------------------------------------------

def _get_state() -> DashboardState:
    if "dash_state" not in st.session_state:
        st.session_state.dash_state = DashboardState()
    return st.session_state.dash_state


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

def main():
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
    if cols[4].button("KILL ALL", type="primary", use_container_width=True):
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
            display_df = df[["Ticker", "Pos", "Entry", "Bid", "Ask", "Mid", "Spread", "MTM", "Status"]].copy()
            display_df["Entry"] = display_df["Entry"].apply(lambda x: f"{x}c" if x else "?")
            display_df["Bid"] = display_df["Bid"].apply(lambda x: f"{x}c" if x is not None else "?")
            display_df["Ask"] = display_df["Ask"].apply(lambda x: f"{x}c" if x is not None else "?")
            display_df["Mid"] = display_df["Mid"].apply(lambda x: f"{x}c" if x is not None else "?")
            display_df["Spread"] = display_df["Spread"].apply(lambda x: f"{x}c" if x is not None else "?")
            display_df["MTM"] = display_df["MTM"].apply(lambda x: f"{x:+d}c")

            st.dataframe(display_df, use_container_width=True, hide_index=True)

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

    # --- Trade panel (bottom) ---
    st.divider()
    with st.expander("Place Order", expanded=False):
        trade_cols = st.columns([3, 1, 1, 1, 1])
        with state.lock:
            all_tickers = sorted(state.subscribed_tickers)
        ticker = trade_cols[0].selectbox("Ticker", all_tickers if all_tickers else ["(loading...)"])
        action = trade_cols[1].selectbox("Action", ["buy", "sell"])
        price = trade_cols[2].number_input("Price (cents)", min_value=1, max_value=99, value=50)
        size = trade_cols[3].number_input("Size", min_value=1, max_value=10, value=1)
        if trade_cols[4].button("Submit Order"):
            try:
                result = place_order(ticker, "yes", action, price, size)
                st.success(f"Order placed: {result}")
            except Exception as e:
                st.error(f"Order failed: {e}")

    # Auto-refresh every 2 seconds
    time.sleep(2)
    st.rerun()


if __name__ == "__main__":
    main()

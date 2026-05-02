# v2/app/clients

API clients for interacting with Kalshi's REST and WebSocket APIs.

## kalshi_sdk.py

Wraps the official `kalshi-python` SDK to discover currently-open markets. Used by the live ingester to build the list of market tickers to subscribe to via WebSocket.

- `make_client()` — creates an authenticated `MarketsApi` instance using RSA-PSS credentials from `.env`
- `paginate_markets()` — cursor-paginated iterator over markets, filtered by series and status

Copied from v1 — identical interface.

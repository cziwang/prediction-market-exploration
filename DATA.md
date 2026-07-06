# Prediction Market Data Reference

All data lives in S3 bucket `prediction-markets-data` (us-east-1).

---

## AWS Setup

- **Credentials:** `~/.aws/credentials` or EC2 IAM role (`prediction-markets-poller`)
- **IAM role permissions:** S3 read/write to `prediction-markets-data`, Athena query, Glue catalog read
- **Athena workgroup:** `prediction-markets`
- **Athena results location:** `s3://prediction-markets-data/athena-results/` (auto-expire 7 days)
- **Glue database:** `prediction_markets`
- **EC2:** t3.small, Ubuntu 24.04, SSH key `~/.ssh/ec2-prediction-market.pem`, last known IP `44.223.221.209`

---

## S3 Structure Overview

```
s3://prediction-markets-data/
├── bronze/
│   ├── kalshi_ws/                        # Raw Kalshi WS frames (gzip-JSONL)
│   │   ├── orderbook_snapshot/YYYY/MM/DD/HH/{uuid}.jsonl.gz
│   │   ├── orderbook_delta/YYYY/MM/DD/HH/{uuid}.jsonl.gz
│   │   └── trade/YYYY/MM/DD/HH/{uuid}.jsonl.gz
│   └── nba_cdn/
│       └── boxscore/YYYY/MM/DD/HH/*.jsonl.gz   # NBA live boxscores
├── silver/
│   └── kalshi_ws/                        # Typed Parquet events (v=3)
│       ├── OrderBookDepth/date=YYYY-MM-DD/v=3/part-*.parquet
│       ├── TradeEvent/date=YYYY-MM-DD/v=3/part-*.parquet
│       ├── BookInvalidated/date=YYYY-MM-DD/v=3/part-*.parquet
│       ├── MMQuoteEvent/date=YYYY-MM-DD/v=3/part-*.parquet
│       ├── MMOrderEvent/date=YYYY-MM-DD/v=3/part-*.parquet
│       ├── MMFillEvent/date=YYYY-MM-DD/v=3/part-*.parquet
│       ├── MMReconcileEvent/date=YYYY-MM-DD/v=3/part-*.parquet
│       └── MMCircuitBreakerEvent/date=YYYY-MM-DD/v=3/part-*.parquet
├── reference/
│   └── kalshi_markets.parquet            # Market metadata (117,900 NBA markets)
├── mm/                                   # Crypto market-maker data
│   ├── bronze/
│   │   ├── deltas/dt=YYYY-MM-DD/*.parquet
│   │   ├── snapshots/dt=YYYY-MM-DD/*.parquet
│   │   └── trades/dt=YYYY-MM-DD/*.parquet
│   └── silver/
│       ├── lob/dt=YYYY-MM-DD/lob.parquet
│       └── trades/dt=YYYY-MM-DD/trades.parquet
├── kalshi/                               # Historical batch-fetched data
│   ├── historical_markets/
│   ├── historical_trades/
│   └── historical_candlesticks/
│       ├── 1m/
│       ├── 60m/
│       └── 1440m/
└── athena-results/                       # Auto-expire 7 days
```

---

## Bronze Layer

### Format
- **gzip-JSONL** — one JSON object per line, gzip compressed
- **Authoritative archive** — never overwrite, silver is rebuildable from this
- **Flush:** 5 MB uncompressed OR 60 seconds, whichever comes first
- **Partitioning:** by receipt time `YYYY/MM/DD/HH/`

### Envelope schema (each line)
```json
{
  "source": "kalshi_ws",
  "channel": "orderbook_delta",
  "market_ticker": "KXNBAGAME-25APR18LAL...",
  "seq": 12345,
  "t_receipt": 1714435200.123,
  "frame": { }
}
```

### Coverage
- **NBA data:** April 2025 – end of 2025-26 season
- **Historical batch fetches:** April 2025 – Feb 16, 2026 (Kalshi historical API cutoff)
- **NBA boxscores:** `bronze/nba_cdn/boxscore/` — live game state snapshots

---

## Silver Layer (v=3)

### Format
- **Parquet**, ZSTD compression, explicit schemas
- **Partitioning:** Hive-style `date=YYYY-MM-DD/v=3/`
- **Timestamps:** `int64` nanoseconds (not float seconds)
- **String columns:** dictionary-encoded (market_ticker, side, etc.)
- **Row groups:** 100,000 rows, sorted by `t_receipt_ns` for predicate pushdown
- **Compaction:** daily at 07:00 UTC, merges `part-*.parquet` → `part-compacted.parquet`

> **Note:** v=2 files (float timestamps, inferred schemas) may exist at `.../v=2/` — deprecated but readable.

---

## Table Schemas

### OrderBookDepth (primary table)
~5M rows/day. One row per book-changing delta. 53 columns.

| Column | Type | Description |
|--------|------|-------------|
| t_receipt_ns | int64 | Our receipt time (nanoseconds, primary sort key) |
| t_exchange_ns | int64 | Kalshi server time (nanoseconds) |
| market_ticker | string | Kalshi market ID (dict-encoded) |
| seq | int32 | Sequence number (gap detection) |
| sid | int32 | Subscription ID (correlates seq numbers) |
| bid_1 … bid_10 | int32 | Bid prices in cents, best→worst (descending) |
| bid_1_size … bid_10_size | int32 | Contracts at each bid level |
| ask_1 … ask_10 | int32 | Ask prices in cents, best→worst (ascending) |
| ask_1_size … ask_10_size | int32 | Contracts at each ask level |
| bid_depth_5c | int32 | Total contracts within 5¢ of best bid |
| ask_depth_5c | int32 | Total contracts within 5¢ of best ask |
| bid_depth_10c | int32 | Total contracts within 10¢ of best bid |
| ask_depth_10c | int32 | Total contracts within 10¢ of best ask |
| num_bid_levels | int32 | Number of active bid levels |
| num_ask_levels | int32 | Number of active ask levels |
| spread | int32 | ask_1 - bid_1 (cents) |
| mid_x2 | int32 | bid_1 + ask_1 (doubled to avoid half-cents) |

**Notes:**
- bid_1 = highest YES price = best bid
- ask_1 = lowest YES price = best ask
- NO book is inverted: `best_ask_no = 100 - max(no_book)`
- All prices in integer cents (1–9900)

---

### TradeEvent
~600K rows/day.

| Column | Type | Description |
|--------|------|-------------|
| t_receipt_ns | int64 | Our receipt time (nanoseconds) |
| t_exchange_ns | int64 | Kalshi server time (nanoseconds, nullable) |
| market_ticker | string | Kalshi market ID (dict-encoded) |
| side | string | "yes" or "no" — taker side |
| price | int32 | Execution price (cents) |
| size | int32 | Contracts traded |
| sid | int32 | Subscription ID (nullable) |
| seq | int32 | Sequence number (nullable) |

---

### BookInvalidated
~50 rows/day. Emitted on reconnect — signals that book state was reset.

| Column | Type | Description |
|--------|------|-------------|
| t_receipt_ns | int64 | Time of reconnect |
| market_ticker | string | Affected market |

---

### MMQuoteEvent
Market-making strategy quotes.

| Column | Type |
|--------|------|
| t_receipt_ns | int64 |
| market_ticker | string |
| bid_price | int32 |
| ask_price | int32 |
| book_bid | int32 |
| book_ask | int32 |
| spread | int32 |
| position | int32 |
| reason_no_bid | string |
| reason_no_ask | string |

---

### MMOrderEvent
Order lifecycle events.

| Column | Type |
|--------|------|
| t_receipt_ns | int64 |
| market_ticker | string |
| action | string |
| price | int32 |
| size | int32 |
| order_id | string |
| reason | string |
| error | string |

---

### MMFillEvent
Fill events with position tracking.

| Column | Type |
|--------|------|
| t_receipt_ns | int64 |
| market_ticker | string |
| side | string |
| price | int32 |
| fill_size | int32 |
| order_remaining_size | int32 |
| position_before | int32 |
| position_after | int32 |
| maker_fee | int32 |
| order_id | string |
| book_mid_at_fill | int32 |

---

### MMReconcileEvent

| Column | Type |
|--------|------|
| t_receipt_ns | int64 |
| market_ticker | string |
| field | string |
| internal_value | string |
| actual_value | string |
| action_taken | string |

---

### MMCircuitBreakerEvent

| Column | Type |
|--------|------|
| t_receipt_ns | int64 |
| state | string |
| consecutive_failures | int32 |
| last_error | string |

---

## Reference Data

### kalshi_markets.parquet
`s3://prediction-markets-data/reference/kalshi_markets.parquet`

117,900 NBA markets, 113,613 with settlement results.

| Column | Type | Description |
|--------|------|-------------|
| ticker | string | Market ID |
| series_ticker | string | Series code (KXNBAGAME, KXNBAPTS, etc.) |
| event_ticker | string | Game or event ID |
| title | string | Market title |
| yes_sub_title | string | |
| no_sub_title | string | |
| status | string | open / cancelled / resolved |
| result | string | yes / no (settled markets) |
| open_time | timestamp UTC | |
| close_time | timestamp UTC | |
| expiration_time | timestamp UTC | |
| settlement_time | timestamp UTC | |
| volume | int64 | Total contracts traded |
| volume_24h | int64 | 24h volume |
| last_price_cents | int32 | Last trade price |
| settlement_value_cents | int32 | Settlement value |

---

## Crypto Market-Maker Data (mm/)

### Bronze schemas

**mm/bronze/deltas/**

| Column | Type |
|--------|------|
| market_ticker | string |
| ts | int64 (epoch millis) |
| side | string |
| price_cents | int32 |
| delta | int32 |
| seq | int32 |
| sid | int32 |

**mm/bronze/snapshots/**

| Column | Type |
|--------|------|
| market_ticker | string |
| ts | int64 (epoch millis) |
| side | string |
| price_cents | int32 |
| qty | int32 |

**mm/bronze/trades/**

| Column | Type |
|--------|------|
| market_ticker | string |
| ts | int64 (epoch millis) |
| yes_price_cents | int32 |
| no_price_cents | int32 |
| count | int32 |
| taker_side | string |

### Silver schemas

**mm/silver/lob/** — Full reconstructed LOB, one row per book-changing tick

| Column | Type |
|--------|------|
| market_ticker | string |
| ts | int64 (epoch millis) |
| side | string |
| price_cents | int32 |
| qty | int32 |

**mm/silver/trades/** — Consolidated, normalized trades

Same schema as bronze/trades/.

**Default markets:** KXBTC, KXETH, KXSOL

---

## Glue Catalog

**Database:** `prediction_markets`

| Table | S3 Path | Partitions |
|-------|---------|------------|
| order_book_depth | silver/kalshi_ws/OrderBookDepth/ | date, v |
| trade_event | silver/kalshi_ws/TradeEvent/ | date, v |
| book_invalidated | silver/kalshi_ws/BookInvalidated/ | date, v |
| mm_quote_event | silver/kalshi_ws/MMQuoteEvent/ | date, v |
| mm_order_event | silver/kalshi_ws/MMOrderEvent/ | date, v |
| mm_fill_event | silver/kalshi_ws/MMFillEvent/ | date, v |
| mm_reconcile_event | silver/kalshi_ws/MMReconcileEvent/ | date, v |
| mm_circuit_breaker_event | silver/kalshi_ws/MMCircuitBreakerEvent/ | date, v |
| market_metadata | reference/ | (none — single file) |

Partition projection enabled (no MSCK REPAIR needed).

---

## NBA Series Coverage

| Series | Type | ~Markets |
|--------|------|----------|
| KXNBAGAME | Win/Loss | 1,902 |
| KXNBATOTAL | Total Points O/U | 9,044 |
| KXNBASPREAD | Point Spread | 8,923 |
| KXNBAPTS | Player Points | 8,876 |
| KXNBAREB | Player Rebounds | 8,481 |
| KXNBA3PT | Player 3-Pointers | 7,804 |
| KXNBAAST | Player Assists | 6,385 |
| KXNBASTL | Player Steals | 1,656 |
| KXNBABLK | Player Blocks | 1,070 |
| KXNBA | Finals Winner | 30 |
| KXNBASERIES | Series Winner | 24 |
| KXNBAPLAYOFF | Playoff Qualifier | 23 |
| KXNBAALLSTAR | All-Star Game | 4 |

**Total:** ~54,000 markets, April 2025 – end of 2025-26 season

---

## Conventions

- **Prices:** integer cents always (1–9900). Never floats.
- **Sizes:** integer contracts always.
- **Timestamps in silver:** `int64` nanoseconds. Bronze uses float seconds.
- **Bronze is authoritative.** Silver is always rebuildable.
- **v=3** is current. v=2 files (float timestamps, inferred schemas) are deprecated but coexist.

---

## S3 Lifecycle Policy

| Layer | Standard | → IA | → Glacier Deep Archive |
|-------|----------|------|----------------------|
| Bronze | 0–90d | 90d | 180d |
| Silver | 0–90d | 90d | — |
| Athena results | 0–7d | (expire) | — |

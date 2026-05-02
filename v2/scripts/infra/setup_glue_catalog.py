"""Create Glue Data Catalog tables and Athena workgroup for silver Parquet data.

Idempotent — safe to run multiple times. Overwrites existing table definitions
so schema changes propagate automatically.

Usage:
    python -m v2.scripts.infra.setup_glue_catalog
    python -m v2.scripts.infra.setup_glue_catalog --dry-run
"""

from __future__ import annotations

import argparse
import logging

import boto3
from botocore.exceptions import ClientError

from v2.app.core.config import S3_BUCKET

log = logging.getLogger(__name__)

DATABASE = "prediction_markets"
ATHENA_WORKGROUP = "prediction-markets"
ATHENA_OUTPUT = f"s3://{S3_BUCKET}/athena-results/"
SILVER_PREFIX = f"s3://{S3_BUCKET}/silver/kalshi_ws"

# Date range start for partition projection — first day of silver data
PROJECTION_DATE_START = "2025-04-01"

# Max bytes scanned per query (10 GB)
BYTES_SCANNED_CUTOFF = 10 * 1024 * 1024 * 1024

# ---------------------------------------------------------------------------
# Arrow-to-Glue type mapping
#
# Glue uses Hive types. Dictionary-encoded strings are still "string" to Glue —
# the dictionary encoding is a Parquet physical detail, invisible at the
# logical/catalog layer.
# ---------------------------------------------------------------------------
GLUE_TABLES: dict[str, list[dict]] = {
    "order_book_update": [
        {"Name": "t_receipt_ns", "Type": "bigint"},
        {"Name": "t_exchange_ns", "Type": "bigint"},
        {"Name": "market_ticker", "Type": "string"},
        {"Name": "bid_yes", "Type": "int"},
        {"Name": "ask_yes", "Type": "int"},
        {"Name": "bid_size", "Type": "int"},
        {"Name": "ask_size", "Type": "int"},
        {"Name": "sid", "Type": "int"},
        {"Name": "seq", "Type": "int"},
    ],
    "trade_event": [
        {"Name": "t_receipt_ns", "Type": "bigint"},
        {"Name": "t_exchange_ns", "Type": "bigint"},
        {"Name": "market_ticker", "Type": "string"},
        {"Name": "side", "Type": "string"},
        {"Name": "price", "Type": "int"},
        {"Name": "size", "Type": "int"},
        {"Name": "sid", "Type": "int"},
        {"Name": "seq", "Type": "int"},
    ],
    "book_invalidated": [
        {"Name": "t_receipt_ns", "Type": "bigint"},
        {"Name": "market_ticker", "Type": "string"},
    ],
    "mm_quote_event": [
        {"Name": "t_receipt_ns", "Type": "bigint"},
        {"Name": "market_ticker", "Type": "string"},
        {"Name": "bid_price", "Type": "int"},
        {"Name": "ask_price", "Type": "int"},
        {"Name": "book_bid", "Type": "int"},
        {"Name": "book_ask", "Type": "int"},
        {"Name": "spread", "Type": "int"},
        {"Name": "position", "Type": "int"},
        {"Name": "reason_no_bid", "Type": "string"},
        {"Name": "reason_no_ask", "Type": "string"},
    ],
    "mm_order_event": [
        {"Name": "t_receipt_ns", "Type": "bigint"},
        {"Name": "market_ticker", "Type": "string"},
        {"Name": "action", "Type": "string"},
        {"Name": "price", "Type": "int"},
        {"Name": "size", "Type": "int"},
        {"Name": "order_id", "Type": "string"},
        {"Name": "reason", "Type": "string"},
        {"Name": "error", "Type": "string"},
    ],
    "mm_fill_event": [
        {"Name": "t_receipt_ns", "Type": "bigint"},
        {"Name": "market_ticker", "Type": "string"},
        {"Name": "side", "Type": "string"},
        {"Name": "price", "Type": "int"},
        {"Name": "fill_size", "Type": "int"},
        {"Name": "order_remaining_size", "Type": "int"},
        {"Name": "position_before", "Type": "int"},
        {"Name": "position_after", "Type": "int"},
        {"Name": "maker_fee", "Type": "int"},
        {"Name": "order_id", "Type": "string"},
        {"Name": "book_mid_at_fill", "Type": "int"},
    ],
    "mm_reconcile_event": [
        {"Name": "t_receipt_ns", "Type": "bigint"},
        {"Name": "market_ticker", "Type": "string"},
        {"Name": "field", "Type": "string"},
        {"Name": "internal_value", "Type": "string"},
        {"Name": "actual_value", "Type": "string"},
        {"Name": "action_taken", "Type": "string"},
    ],
    "mm_circuit_breaker_event": [
        {"Name": "t_receipt_ns", "Type": "bigint"},
        {"Name": "state", "Type": "string"},
        {"Name": "consecutive_failures", "Type": "int"},
        {"Name": "last_error", "Type": "string"},
    ],
}

# Map snake_case table name -> PascalCase S3 directory name
_TABLE_TO_S3_DIR: dict[str, str] = {
    "order_book_update": "OrderBookUpdate",
    "trade_event": "TradeEvent",
    "book_invalidated": "BookInvalidated",
    "mm_quote_event": "MMQuoteEvent",
    "mm_order_event": "MMOrderEvent",
    "mm_fill_event": "MMFillEvent",
    "mm_reconcile_event": "MMReconcileEvent",
    "mm_circuit_breaker_event": "MMCircuitBreakerEvent",
}

_TABLE_DESCRIPTIONS: dict[str, str] = {
    "order_book_update": "BBO snapshots and deltas from Kalshi orderbook_delta/orderbook_snapshot channels",
    "trade_event": "Executed trades from Kalshi trade channel",
    "book_invalidated": "Book invalidation signals requiring full orderbook resync",
    "mm_quote_event": "Market maker quoting decisions with book state and skip reasons",
    "mm_order_event": "Market maker order lifecycle events (place, cancel, replace)",
    "mm_fill_event": "Market maker fill notifications with position tracking",
    "mm_reconcile_event": "Internal vs exchange state mismatches detected during reconciliation",
    "mm_circuit_breaker_event": "Circuit breaker state transitions triggered by consecutive failures",
}


def _table_input(table_name: str, columns: list[dict]) -> dict:
    """Build Glue CreateTable/UpdateTable input for one event type."""
    s3_dir = _TABLE_TO_S3_DIR[table_name]
    location = f"{SILVER_PREFIX}/{s3_dir}/"

    return {
        "Name": table_name,
        "Description": _TABLE_DESCRIPTIONS.get(table_name, ""),
        "StorageDescriptor": {
            "Columns": columns,
            "Location": location,
            "InputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetInputFormat",
            "OutputFormat": "org.apache.hadoop.hive.ql.io.parquet.MapredParquetOutputFormat",
            "SerdeInfo": {
                "SerializationLibrary": "org.apache.hadoop.hive.ql.io.parquet.serde.ParquetHiveSerDe",
                "Parameters": {"serialization.format": "1"},
            },
            "Compressed": True,
        },
        "PartitionKeys": [
            {"Name": "date", "Type": "string"},
            {"Name": "v", "Type": "int"},
        ],
        "TableType": "EXTERNAL_TABLE",
        "Parameters": {
            # Partition projection — Athena computes partitions from these
            # rules instead of listing S3 or querying the Glue partition API.
            "projection.enabled": "true",
            "projection.date.type": "date",
            "projection.date.format": "yyyy-MM-dd",
            "projection.date.range": f"{PROJECTION_DATE_START},NOW",
            "projection.date.interval": "1",
            "projection.date.interval.unit": "DAYS",
            "projection.v.type": "integer",
            "projection.v.range": "3,3",
            "storage.location.template": f"{location}date=${{date}}/v=${{v}}/",
            "classification": "parquet",
        },
    }


def create_database(glue, *, dry_run: bool) -> None:
    """Create the Glue database if it doesn't exist."""
    if dry_run:
        log.info("[DRY RUN] would create database %s", DATABASE)
        return

    try:
        glue.create_database(DatabaseInput={"Name": DATABASE})
        log.info("created database %s", DATABASE)
    except ClientError as e:
        if e.response["Error"]["Code"] == "AlreadyExistsException":
            log.info("database %s already exists", DATABASE)
        else:
            raise


def create_or_update_table(glue, table_name: str, columns: list[dict], *, dry_run: bool) -> None:
    """Create or update a single Glue table."""
    table_input = _table_input(table_name, columns)

    if dry_run:
        log.info("[DRY RUN] would create/update table %s.%s (%d columns)",
                 DATABASE, table_name, len(columns))
        log.info("  location: %s", table_input["StorageDescriptor"]["Location"])
        return

    try:
        glue.create_table(DatabaseName=DATABASE, TableInput=table_input)
        log.info("created table %s.%s", DATABASE, table_name)
    except ClientError as e:
        if e.response["Error"]["Code"] == "AlreadyExistsException":
            glue.update_table(DatabaseName=DATABASE, TableInput=table_input)
            log.info("updated table %s.%s", DATABASE, table_name)
        else:
            raise


def create_athena_workgroup(athena, *, dry_run: bool) -> None:
    """Create the Athena workgroup with cost guard."""
    if dry_run:
        log.info("[DRY RUN] would create workgroup %s (output: %s, cutoff: %s GB)",
                 ATHENA_WORKGROUP, ATHENA_OUTPUT,
                 BYTES_SCANNED_CUTOFF / (1024 ** 3))
        return

    config = {
        "ResultConfiguration": {"OutputLocation": ATHENA_OUTPUT},
        "EnforceWorkGroupConfiguration": True,
        "BytesScannedCutoffPerQuery": BYTES_SCANNED_CUTOFF,
        "EngineVersion": {"SelectedEngineVersion": "Athena engine version 3"},
    }

    try:
        athena.create_work_group(
            Name=ATHENA_WORKGROUP,
            Configuration=config,
        )
        log.info("created workgroup %s", ATHENA_WORKGROUP)
    except ClientError as e:
        if e.response["Error"]["Code"] == "InvalidRequestException" and "already" in str(e):
            athena.update_work_group(
                WorkGroup=ATHENA_WORKGROUP,
                ConfigurationUpdates={
                    "ResultConfigurationUpdates": {"OutputLocation": ATHENA_OUTPUT},
                    "EnforceWorkGroupConfiguration": True,
                    "BytesScannedCutoffPerQuery": BYTES_SCANNED_CUTOFF,
                    "EngineVersion": {"SelectedEngineVersion": "Athena engine version 3"},
                },
            )
            log.info("updated workgroup %s", ATHENA_WORKGROUP)
        else:
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Set up Glue catalog + Athena workgroup")
    parser.add_argument("--dry-run", action="store_true", help="Print what would be done without making changes")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    glue = boto3.client("glue")
    athena = boto3.client("athena")

    create_database(glue, dry_run=args.dry_run)

    for table_name, columns in GLUE_TABLES.items():
        create_or_update_table(glue, table_name, columns, dry_run=args.dry_run)

    create_athena_workgroup(athena, dry_run=args.dry_run)

    log.info("done — %d tables in %s, workgroup %s",
             len(GLUE_TABLES), DATABASE, ATHENA_WORKGROUP)


if __name__ == "__main__":
    main()

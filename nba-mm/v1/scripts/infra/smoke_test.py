"""End-to-end smoke test for BronzeWriter and SilverWriter.

Writes synthetic records under a one-off `smoketest_{ts}` source prefix,
verifies the S3 objects round-trip, then deletes them.

    python -m scripts.infra.smoke_test
"""

from __future__ import annotations

import asyncio
import gzip
import io
import json
import logging
import time

import boto3
import pyarrow.parquet as pq

from app.core.config import S3_BUCKET
from app.events import OrderBookUpdate, TradeEvent
from app.services.bronze_writer import BronzeWriter
from app.services.silver_writer import SilverWriter

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smoke_test")


async def smoke_bronze(source: str) -> list[str]:
    async with BronzeWriter(source=source, flush_seconds=3600) as bronze:
        for i in range(5):
            await bronze.emit(
                {
                    "source": source,
                    "channel": "orderbook_delta",
                    "seq": i,
                    "t_receipt": time.time(),
                    "frame": {"market_ticker": "KX-TEST", "delta": [[50, 3]]},
                },
                channel="orderbook_delta",
            )
        for i in range(3):
            await bronze.emit(
                {
                    "source": source,
                    "channel": "trade",
                    "seq": i,
                    "t_receipt": time.time(),
                    "frame": {"market_ticker": "KX-TEST", "price": 52, "size": 1},
                },
                channel="trade",
            )
    return ["orderbook_delta", "trade"]


async def smoke_silver(source: str) -> list[str]:
    async with SilverWriter(source=source, flush_seconds=3600) as silver:
        now = time.time()
        for i in range(5):
            await silver.emit(OrderBookUpdate(
                t_receipt=now + i,
                market_ticker="KX-TEST",
                bid_yes=50, ask_yes=52, bid_size=10, ask_size=8,
            ))
        for i in range(3):
            await silver.emit(TradeEvent(
                t_receipt=now + i,
                market_ticker="KX-TEST",
                side="yes", price=51, size=1,
            ))
    return ["OrderBookUpdate", "TradeEvent"]


def verify_bronze(s3, source: str, channels: list[str]) -> list[str]:
    keys_to_delete = []
    for channel in channels:
        prefix = f"bronze/{source}/{channel}/"
        objs = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix).get("Contents", [])
        assert objs, f"no bronze objects under {prefix}"
        for obj in objs:
            key = obj["Key"]
            body = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
            lines = gzip.decompress(body).decode().splitlines()
            records = [json.loads(line) for line in lines]
            assert records and all(r["channel"] == channel for r in records)
            log.info("bronze OK: %s (%d records)", key, len(records))
            keys_to_delete.append(key)
    return keys_to_delete


def verify_silver(s3, source: str, type_names: list[str]) -> list[str]:
    keys_to_delete = []
    for type_name in type_names:
        prefix = f"silver/{source}/{type_name}/"
        objs = s3.list_objects_v2(Bucket=S3_BUCKET, Prefix=prefix).get("Contents", [])
        assert objs, f"no silver objects under {prefix}"
        for obj in objs:
            key = obj["Key"]
            body = s3.get_object(Bucket=S3_BUCKET, Key=key)["Body"].read()
            table = pq.read_table(io.BytesIO(body))
            assert table.num_rows > 0
            log.info("silver OK: %s (%d rows, cols=%s)",
                     key, table.num_rows, table.column_names)
            keys_to_delete.append(key)
    return keys_to_delete


def cleanup(s3, keys: list[str]) -> None:
    if not keys:
        return
    s3.delete_objects(
        Bucket=S3_BUCKET,
        Delete={"Objects": [{"Key": k} for k in keys]},
    )
    log.info("cleaned up %d objects", len(keys))


async def main() -> None:
    ts = int(time.time())
    source = f"smoketest_{ts}"
    s3 = boto3.client("s3")

    log.info("running smoke test under source=%s bucket=%s", source, S3_BUCKET)

    bronze_channels = await smoke_bronze(source)
    silver_types = await smoke_silver(source)

    bronze_keys = verify_bronze(s3, source, bronze_channels)
    silver_keys = verify_silver(s3, source, silver_types)

    cleanup(s3, bronze_keys + silver_keys)

    log.info("smoke test passed ✓")


if __name__ == "__main__":
    asyncio.run(main())

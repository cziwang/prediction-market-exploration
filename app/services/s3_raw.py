"""S3 raw data storage.

Stores raw API responses as JSON in S3 with deterministic keys
to avoid duplicates on re-fetch.
"""

from __future__ import annotations

import json

import boto3

from app.core.config import S3_BUCKET

_client = None


def _get_client():
    global _client
    if _client is None:
        _client = boto3.client("s3")
    return _client


def put_raw(source: str, dataset: str, key: str, data: object) -> str:
    """Write raw data to S3.

    Path: s3://{bucket}/{source}/{dataset}/{key}
    Deterministic key means re-calling with same args overwrites, no duplicates.

    Args:
        source: Data source, e.g. "nba", "kalshi"
        dataset: Dataset name, e.g. "games", "markets"
        key: Filename, e.g. "season_2024-25.json"
        data: JSON-serializable object

    Returns:
        The full S3 key that was written.
    """
    s3_key = f"{source}/{dataset}/{key}"
    _get_client().put_object(
        Bucket=S3_BUCKET,
        Key=s3_key,
        Body=json.dumps(data, default=str),
        ContentType="application/json",
    )
    return s3_key


def list_keys(source: str, dataset: str) -> list[str]:
    """List all keys under a source/dataset prefix."""
    prefix = f"{source}/{dataset}/"
    keys: list[str] = []
    paginator = _get_client().get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=S3_BUCKET, Prefix=prefix):
        for obj in page.get("Contents", []):
            keys.append(obj["Key"])
    return keys


def get_raw(s3_key: str) -> object:
    """Read and parse a JSON object from S3."""
    resp = _get_client().get_object(Bucket=S3_BUCKET, Key=s3_key)
    return json.loads(resp["Body"].read())

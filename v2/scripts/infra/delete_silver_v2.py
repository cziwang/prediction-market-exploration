"""Delete all silver v=1 and v=2 files from S3.

v=1 and v=2 are fully superseded by v=3 (backfilled from bronze). This script
lists all non-v=3 objects, shows a summary, and deletes after confirmation.

Usage:
    python -m v2.scripts.infra.delete_silver_v2 --dry-run
    python -m v2.scripts.infra.delete_silver_v2
"""

from __future__ import annotations

import argparse
import logging

import boto3

from v2.app.core.config import S3_BUCKET

log = logging.getLogger(__name__)

SOURCE = "kalshi_ws"
OLD_VERSIONS = {1, 2}


def find_old_keys(s3, bucket: str) -> list[dict]:
    """Find all silver v=1 and v=2 objects. Returns list of {Key, Size}."""
    prefix = f"silver/{SOURCE}/"
    paginator = s3.get_paginator("list_objects_v2")
    keys = []
    for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
        for obj in page.get("Contents", []):
            for v in OLD_VERSIONS:
                if f"/v={v}/" in obj["Key"]:
                    keys.append({"Key": obj["Key"], "Size": obj["Size"]})
                    break
    return keys


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    parser = argparse.ArgumentParser(description="Delete silver v=1 and v=2 files from S3")
    parser.add_argument("--bucket", default=S3_BUCKET)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    s3 = boto3.client("s3")
    keys = find_old_keys(s3, args.bucket)

    if not keys:
        log.info("no v=1 or v=2 files found")
        return

    total_bytes = sum(k["Size"] for k in keys)
    log.info("found %d old files (%.2f MB)", len(keys), total_bytes / (1024 * 1024))
    for k in keys:
        log.info("  %s (%d bytes)", k["Key"], k["Size"])

    if args.dry_run:
        log.info("[DRY RUN] would delete %d files", len(keys))
        return

    confirm = input(f"\nDelete {len(keys)} files? [y/N] ")
    if confirm.lower() != "y":
        log.info("aborted")
        return

    # S3 delete_objects supports up to 1000 keys per call
    for i in range(0, len(keys), 1000):
        batch = [{"Key": k["Key"]} for k in keys[i:i + 1000]]
        s3.delete_objects(Bucket=args.bucket, Delete={"Objects": batch})

    log.info("deleted %d files", len(keys))


if __name__ == "__main__":
    main()

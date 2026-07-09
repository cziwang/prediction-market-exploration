"""Wait for Kafka topic deletion to complete (async operation).

Usage: python scripts/wait_topics_deleted.py topic1 topic2 ...

Polls the broker until none of the named topics exist, or times out after 60s.
Used by `make wipe-topics` — see war story #4 in TESTING.md for why this matters.
"""

import sys
import time

from confluent_kafka.admin import AdminClient


def main() -> None:
    targets = set(sys.argv[1:])
    if not targets:
        return
    admin = AdminClient({"bootstrap.servers": "localhost:9092"})
    for _ in range(60):
        if not (set(admin.list_topics(timeout=5).topics) & targets):
            print("deletion complete")
            return
        time.sleep(1)
    sys.exit("deletion did not complete")


if __name__ == "__main__":
    main()

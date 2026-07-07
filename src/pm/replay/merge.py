"""K-way merge of bronze sources by receipt timestamp (DESIGN.md §1).

Classic merge-k-sorted-lists via a heap: hold one record per source, pop the
smallest timestamp, refill from the source it came from. Memory is O(k) for
k sources regardless of file sizes.

Ordering within each source is verified, not assumed: bronze files should be
internally sorted by t_receipt, but a source that yields a decreasing
timestamp aborts the merge with OrderingError and a precise report. Producing
out-of-order data to Kafka would silently corrupt Flink's watermarks
downstream — failing loudly here is the cheap place to catch it.

Ties are broken by source index (deterministic: same inputs, same output
order — required for golden-file reproducibility).
"""

import heapq
from collections.abc import Iterator

from pm.replay.sources import BronzeSource, SourceRecord


class OrderingError(Exception):
    """A source yielded a record with t_receipt_ns less than its predecessor."""

    def __init__(self, source_id: str, prev_ns: int, current_ns: int) -> None:
        self.source_id = source_id
        self.prev_ns = prev_ns
        self.current_ns = current_ns
        super().__init__(
            f"{source_id}: t_receipt_ns went backward "
            f"({prev_ns} -> {current_ns}, delta {current_ns - prev_ns}ns)"
        )


def _verified(source: BronzeSource) -> Iterator[SourceRecord]:
    """Wrap a source's iterator, asserting non-decreasing timestamps."""
    prev_ns: int | None = None
    for record in source.records():
        if prev_ns is not None and record.t_receipt_ns < prev_ns:
            raise OrderingError(source.source_id, prev_ns, record.t_receipt_ns)
        prev_ns = record.t_receipt_ns
        yield record


def merge_sources(sources: list[BronzeSource]) -> Iterator[SourceRecord]:
    """Yield records from all sources in globally non-decreasing t_receipt_ns order.

    Raises OrderingError if any single source is internally out of order.
    """
    # Heap entries: (t_receipt_ns, source_index, record).
    # source_index breaks timestamp ties deterministically and prevents heapq
    # from ever comparing SourceRecord objects (which are not orderable).
    iterators = [_verified(source) for source in sources]
    heap: list[tuple[int, int, SourceRecord]] = []

    for index, iterator in enumerate(iterators):
        first = next(iterator, None)
        if first is not None:
            heap.append((first.t_receipt_ns, index, first))
    heapq.heapify(heap)

    while heap:
        _, index, record = heapq.heappop(heap)
        yield record
        refill = next(iterators[index], None)
        if refill is not None:
            heapq.heappush(heap, (refill.t_receipt_ns, index, refill))

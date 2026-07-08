# Technical Learnings

Challenges encountered while building this project, written to answer interview-style questions
like "what technical problems did you run into?"

---

## PyFlink state serialization is O(n²) when you store a growing buffer as a single ValueState

### What happened

The enrichment job was taking ~6 hours to process 3M records (~140 rec/s). The bottleneck was
not Kafka, not the join logic, and not network I/O — it was how the Flink operator persisted
state.

### The setup

The `AsOfJoinOperator` wraps a Python object (`AsOfJoiner`) that holds a heap buffer of all
records that haven't been emitted yet. Records stay in the buffer until the watermark advances
past them. The game_state source uses a 30-second out-of-orderness window, so during dense
trading, hundreds of records accumulate in the buffer simultaneously.

The operator stored the entire `AsOfJoiner` as a single `ValueState<PICKLED_BYTE_ARRAY>`:

```python
def process_element1(self, tagged, ctx):
    joiner = self._state.value()   # unpickle the entire AsOfJoiner
    joiner.on_trade(tagged)        # add one item to joiner._buffer
    self._state.update(joiner)     # re-pickle the entire AsOfJoiner
```

### Why it's O(n²)

Pickling a Python object serializes everything inside it, including every item currently in
`_buffer`. So when trade #N arrives and the buffer already holds N-1 items, Flink serializes
all N-1 existing items plus the new one.

Across a burst of N trades in one 30-second window:

```
Trade 1:  serialize 1 item
Trade 2:  serialize 2 items
Trade 3:  serialize 3 items
...
Trade N:  serialize N items

Total work = 1 + 2 + 3 + ... + N = N(N+1)/2  →  O(n²)
```

Every already-buffered trade gets re-serialized on every subsequent arrival. A playoff game
with dense trading (thousands of records per 30s window) pushes this into brutal territory.

### The fix

Replace `ValueState<pickled object>` with Flink's native granular state types:

- `ListState` for the buffer — Flink appends each new item as a separate serialized entry
  without touching existing ones. Each arrival costs O(1) instead of O(n).
- Small `ValueState` objects for `_receipt_state` and `_event_state` — these change rarely
  (only on game state updates) and are small, so pickling them is fine.

With `ListState`, total serialization work across N arrivals is O(n) — each item serialized
exactly once.

### The broader lesson

Flink's built-in state types (`ListState`, `MapState`, `ValueState` with a registered
serializer) are designed to be updated incrementally. Storing a Python object as a pickled
blob trades away all of that — every update rewrites the entire blob. If the blob contains a
growing collection, you get quadratic behavior. Always match the state type to the access
pattern: appending → `ListState`, key lookup → `MapState`, single scalar → `ValueState`.

---

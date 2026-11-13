# Reference match provider

This standalone provider illustrates **allocation-owned scientific setup reused
across independent analyses** in [the end-state boundary](../../notes/066_provider_execution_end_state.txt).
It complements the dummy's prepared-state flow and the spectrum provider's
portable result transfer.

```text
src/reference_provider/
├── science.py       immutable reference data, inverted index and direct query
├── binding.py       configured contribution and allocation-owned index lifecycle
├── settings.toml    reference asset and matching threshold
├── resources.toml   scientific CPU requirement
└── __main__.py      local deployment and two sequential analyses
```

`build_index(references)` normalizes the configured reference vectors and builds
channel-to-reference weighted postings. `match(index, settings, candidate)`
normalizes a query and accumulates cosine scores through those postings.
These ordinary functions know no Jobs, backend, allocations or operation handles.
Channel identity is the vector position; all vectors use the same dimension.
Values must be finite and nonnegative. Zero queries, dimension mismatches, no
overlap and scores below the configured threshold are explicit rejections.
Exact score ties choose the lexicographically smallest label.

`create_binding()` captures immutable settings and the reference asset in the
binding revision. It describes one `reference.match/match` call. The instance
initializer builds the actual index once; retirement disposes that retained
index. Each query is self-contained, so there are no analysis hooks, scratch
workspace or prepared-state handles. A completed report contains only a label
and score and needs no live index to be read.

The executable reserves one fixed CPU allocation. It closes and observes the
first analysis before opening the second on that same initialized allocation.
The first result stays retained and readable while the second query runs.
Finally, SDK `ResultConsumption` owns each report's read/consumer attempt and
releases its exact result only after consumer success. A cancelled waiter can
join that original attempt; a failed receiver is never automatically invoked
again. The SDK owner's shutdown join finishes each original acceptance and
release observation before host teardown, deferring observer cancellation until
the result or failure is known. Generic
observation and consumption machinery belong to the SDK. The bootstrap observes
host retirement separately. Cancelling the executable initiates its orderly
shutdown; it does not promise restart recovery or forced interruption.
A later analysis failure still allows an earlier successful report to reach the
consumer. Failed consumption leaves that result retained until process exit;
there is no persistent owner that could recover it after this demo exits.

The four reference patterns are **synthetic illustrations**. This example makes
no claim about chemical identification, validated NMR behavior or performance.
It implements the scientific contribution and execution boundary over the real
`EmbeddedHost`; it does not implement the future common Jobs service, concurrent
analysis sharing, durable publication or remote execution.

From the repository root, qualify all independently installed examples with:

```sh
make tests/integration/provider-core DOCKER_CONTEXT=science WHEELHOUSE=/home/ra/Downloads/wheelhouse
```

With this example and the matching candidate SDK installed inside a permitted
container, run `python -I -B -m reference_provider`. It prints two JSON reports:
`pattern-a` for `(3, 4, 0, 0)` and `pattern-b` for `(0, 0, 1, 1)`, each scoring
approximately `1`. Flushed stdout is the local consumer; it is not a durable
report queue or proof of central API publication.

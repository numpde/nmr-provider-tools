# Reference match provider

Reference demonstrates one allocation-owned reference index reused across
sequential analyses on either the SDK thread host or persistent process host.
Science, operation declarations and request sequencing are identical on both
backends. The deployment selects `execution.backend = "thread"` or `"process"`.

```text
config/deployments/localhost/provider.toml   science, resources and execution policy
src/reference_provider/
├── science.py          immutable reference data, index and cosine matching
├── binding.py          operation declaration and index initialization/cleanup
├── provider.py         deployment selection and SDK host lifetime
├── process_factory.py  installed construction from settings and references
├── application.py      sequential queries and retained report delivery
└── __main__.py         two-query demo with flushed stdout consumption
```

`open_provider()` reads the external deployment document once. Thread startup
adopts the configured matcher and builds its index lazily on first use. Process
startup calls the installed `reference_provider.process_factory:construct`
with explicit settings and reference records, then initializes the child before
yielding. This readiness difference follows the two SDK startup contracts.
`ReferenceMatcher.initialize()` builds the index once and `close()` releases it.
The same installed constructor assembles the scientific configuration for both
backends; scientific records remain provider-owned.

Process deployment calls `open_provider(process_session=session)` with an
existing deployment-owned `RunnerSession` for the SDK scientific worker
(`python -I -B -m provider_core.process_worker`). Creating `ProcessExecutionHost`
transfers session ownership to the SDK; the host joins retirement on context
exit. Reference does not create a supervisor or infer its socket and lock
identity. Thread startup refuses an unrelated process session; process startup
requires one. Each provider context owns exactly one allocation and one host.
Declared CPU requirements record the environment's obligation; they do not
allocate hardware or prove availability.

`run_queries(provider=..., candidates=..., receive=...)` sequences independent
analyses through typed call bindings and the SDK `Submission`. It closes the
first analysis before opening the second on the same allocation. Completed
reports remain independently owned and readable while later queries run.
A cancelled `Submission.wait()` can observe its original operation again;
submitting that retained owner returns the acquired operation. An uncertain
reply does not authorize a fresh request. No backend branch appears in the
request workflow.

A later scientific failure still allows earlier reports to reach the receiver
before waiting for the failed analysis's cleanup. SDK `ResultConsumption` owns
each receiver attempt and releases the exact result after success. Receiver
failures are not retried automatically. Query and analysis-cleanup observation
waits have 15-second caller caps; host exit joins original retirement even when
settlement takes longer. Cancelled waits do not force science to stop. There is
no restart recovery or durable delivery owner after this demo exits.

`build_index(reference_set)` normalizes references and builds weighted channel
postings. `match(index, settings, candidate)` computes cosine scores. Vector
positions identify channels; values must be finite and nonnegative and
references have equal dimensions. Zero queries, dimension mismatches, no overlap
and scores below the threshold are scientific rejections. Exact score ties
choose the smallest label. Scientific functions know no allocations or backend.
The four reference patterns are synthetic illustrations, with no claim about
chemical identification or validated NMR performance.

With this example and the matching SDK installed inside a permitted container,
run `python -I -B -m reference_provider` from a directory containing the supplied
`config/deployments/localhost/provider.toml`. It prints JSON reports for
`pattern-a` and `pattern-b`, each scoring approximately `1`. The document stays
outside the wheel and `PROVIDER_CONFIG_PATH` selects it. The command uses the
supplied thread deployment; an enclosing service supplies the native session
when choosing process startup. Flushed stdout is local consumption, not durable
publication.

From the repository root, run the installed two-backend proof with:

```sh
make tests/integration/provider-core DOCKER_CONTEXT=science WHEELHOUSE=/home/ra/Downloads/wheelhouse PRIVACY_TESTS=0 TEST_PATTERN=test_reference_backends.py
```

The integration fixture supplies the existing native supervisor and exercises
the actual installed process factory, sequential queries, retained results,
scientific rejection, cancelled observation and worker retirement. Existing
binding and workflow tests additionally prove lazy initialization, index
identity reuse and earlier delivery before failed-query cleanup. Real-model,
GPU and deployment qualification remain separate gates.

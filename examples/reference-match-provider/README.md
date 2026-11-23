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
├── application.py      sequential typed calls and report delivery
└── __main__.py         two-query demo with flushed stdout consumption
```

`open_provider()` reads the external deployment document once and enters
`ExecutionSettings.open_contribution()`. The SDK constructs the selected host,
initializes the reference index, and retires it on exit on either backend.
The example supplies the scientific class, its installed constructor and its
configuration. A process deployment injects a fresh `RunnerSession`; the common
SDK context takes custody on successful host construction.

`run_queries(provider=..., candidates=..., receive=...)` is a loop over
`await provider.match(scientific_input=candidate, receive=receive)`. The SDK
owns admission, waiting, decoding, receiver settlement, result release and analysis
cleanup. Each report reaches the receiver before the next query starts, so a later
failure cannot suppress it. The loaded reference index is reused between calls.
Cancellation joins an already started receiver before cleanup. Receiver failures
are never retried automatically; this demo provides no durable delivery after exit.

The deployment's `[execution.error_policy]` selects bounded backoff retries for
confirmed capacity/unavailability refusals. Set `max_attempts = 1` to raise after
one attempt. Scientific calculations and receiver effects are not replayed by
this deployment. The SDK's lower-level interfaces remain demonstrated by the
backend tests, where result handles deliberately outlive their analyses.

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
binding and workflow tests additionally prove initialization, index
identity reuse and earlier delivery before failed-query cleanup. Real-model,
GPU and deployment qualification remain separate gates.

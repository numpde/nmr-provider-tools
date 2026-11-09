# Dummy provider example

This package is the complete example of a scientific contribution and its
provider-owned conformance scenarios. It is not a provider service or a miniature
copy of the shared platform.

`science.py` defines provider-owned scientific types and functions. Portable
configuration, inputs and results are immutable dataclasses. Live model,
analysis and prepared state are separate mutable objects with explicit
lifetimes. Its only provider-core dependency is the lightweight
`ScientificRejection` contract in `provider_core.contribution_errors`; importing
the science does not import the host interface or require the contained runtime
launcher.

`binding.py` connects that science to `provider_core.contribution`. Shared codecs
admit bytes into the exact provider type before science runs. Calls that happen
to have the same JSON shape still use different types and format identities when
they mean different things; for example, `PreparationInput` is not `SumInput`.
Retained worker-local state is bound into `CalculationState`, rather than exposed
as an untyped mapping. Calls with no portable fields use the shared `NoInput`
marker and shared `provider-core.no-input.v1` wire format; calls with no retained
state use the shared `NoState` marker. The provider defines neither empty records
nor provider-specific empty formats.

`conformance.py` completes the example with provider-owned binding factories,
exact wire inputs, expected wire results, reviewed rejection codes, progress
facts, allocation choices and retained-state handoffs. It declares scenarios; it
does not allocate resources, dispatch operations or implement hosting.

`ScientificInputRejected` derives directly from `ScientificRejection`. The
adapter therefore lets a reviewed scientific refusal cross the contribution
boundary unchanged; it does not catch and reconstruct the exception.

Each scientific call is declared once with
`contribution.CallBinding.from_json_dataclasses(...)`. Its adapter uses required
keyword-only parameters selected from `instance`, `analysis`, `scientific_input`,
`state` and `control`, and declares only the values or narrow capabilities it
actually consumes. The dummy's prepare adapter receives `analysis` and
`scientific_input`; calculate receives `analysis`, `state` and `control`; the
stateless sum receives only `scientific_input`.

The shared constructor derives the portable codecs and retained-state binding
from those annotations, then creates the scheduler-facing `CallDescription` from
the same statement. `contribution.ImplementationBinding.from_calls(...)` likewise
derives the implementation's call descriptions from the bound calls. It checks
any requested instance and analysis types against the lifecycle hooks. Unannotated,
misordered, positional, unknown or operationally unjustified adapter parameters
are rejected when the binding is constructed, before scientific resources are
acquired. Simple results use `ScientificReturn(value=...)`; empty state and
missing disposal callbacks are shared defaults rather than repeated adapter
boilerplate.

Canonical configuration and asset descriptions, byte digests and configured
binding revisions are built by `provider_core.contribution_declaration`.
`binding.py` supplies the provider-owned values and semantic identities; it does
not implement generic JSON encoding or hashing. The stateless sum declares
`configuration=None` instead of inventing an empty configuration record. Callers
use `sum_binding` and `weighted_sum_binding` to construct the two example
implementations.

The weighted example declares active CPU work but does not invent a resident
resource quantity for its tiny coefficient tuple. Shared backend tests add a
synthetic resident requirement when exercising grant refusal; the example itself
describes only requirements it can state meaningfully.

The contribution supplies configuration and asset identities, named scientific
calls, resource requirements, lifecycle hooks, result encoding and scientific
cleanup. It supplies none of the operational platform: no CLI, Jobs client,
interpreter, scheduler, execution host, pool, allocator, permission owner,
supervision, recovery, delivery, drain or deployment code.

## Provider-modular conformance

`provider_core.provider_conformance.ProviderConformanceTests` owns the generic
execution checks. The example's `conformance.py` supplies only the scientific
facts those checks cannot know: configured binding factories, exact inputs and
outputs, reviewed rejection codes, progress and state handoffs.
`tests/integration/test_dummy_provider_conformance.py` only applies the shared
test case to this packaged example.

The shared runner creates fixed allocations, submits identified calls, observes
results and progress, releases retained results, closes analyses and retires the
host. The example runs the weighted implementation first with one allocation and
then with two allocations of the same binding, without changing scientific code.
It also exercises rejected and corrected preparation, retained prepared state,
progress and the stateless sum.

A real provider can supply the equivalent scenario declaration alongside its own
contribution while reusing the same shared execution assertions.

# Dummy scientific contribution

This package shows the code a scientific provider contributes. It is not a
provider service or a miniature copy of the shared platform.

`science.py` defines provider-owned scientific types and functions. Portable
configuration, inputs and results are immutable dataclasses. Live model,
analysis and prepared state are separate mutable objects with explicit
lifetimes. The module imports no `provider_core` code and can be called directly.

`binding.py` connects that science to `provider_core.contribution`. Shared codecs
admit bytes into the exact provider type before science runs. Calls that happen
to have the same JSON shape still use different types and format identities when
they mean different things; for example, `PreparationInput` is not `SumInput`.
Retained worker-local state is bound into `CalculationState`, rather than exposed
as an untyped mapping.

Each scientific call is declared once with
`contribution.CallBinding.from_json_dataclasses(...)`. The shared constructor
derives its input codec, retained-state binding and result codec from the
scientific function's annotations, then creates the scheduler-facing
`CallDescription` from the same statement.
`contribution.ImplementationBinding.from_calls(...)` likewise derives the
implementation's call descriptions from the bound calls.

Canonical configuration and asset descriptions, byte digests and configured
binding revisions are built by `provider_core.contribution_declaration`.
`binding.py` supplies the provider-owned values and semantic identities; it does
not implement generic JSON encoding or hashing. The adapter exposes only
`sum_binding` and `weighted_sum_binding` as its public API.

The contribution supplies configuration and asset identities, named scientific
calls, resource requirements, lifecycle hooks, result encoding and scientific
cleanup. It supplies none of the operational platform: no CLI, Jobs client,
interpreter, scheduler, execution host, pool, allocator, permission owner,
supervision, recovery, delivery, drain or deployment code.

Shared tests create allocations, submit fake work, enforce permission, retain
results and perform cleanup. A real provider replaces the dummy scientific types
and functions while keeping the same ownership boundary.

# Dummy provider example

This directory is a standalone consumer of `provider-core`. It is deliberately
shaped like a new scientific-provider repository, not like an internal module of
the shared package.

```text
src/dummy_provider/
    science.py       provider-owned types, lifetimes, validation and calculation
    binding.py       create_*_binding factories for registered implementations
tests/
    conformance.py   provider-owned end-to-end scenarios and expected outcomes
    test_science.py  direct scientific behavior without an execution host
    test_binding.py  provider-specific binding and package-boundary checks
    test_conformance.py
                     shared execution assertions applied to these scenarios
```

The provider contributes no scheduler, Jobs client, execution host, pool,
allocator, permission owner, recovery loop, result delivery, drain or deployment
machinery. Shared code supplies those responsibilities.

## Starting another provider

Copy this directory into the new provider repository. Rename the distribution and
import package, replace `science.py`, then adapt the `create_*_binding` factories.
Keep scientific configuration and assets in the contribution, and keep
orchestration authority out of scientific functions. Replace the scenarios in
`tests/conformance.py` with provider-owned wire inputs, exact expected outputs,
reviewed scientific rejection codes, progress facts and retained-state handoffs.

Do not copy shared allocation, dispatch, observation or cleanup assertions. The
`ProviderConformanceTests` mixin in `provider_core.testing.conformance` supplies
those checks for every provider.

In this repository, the installed integration lane builds the candidate
`provider-core` wheel, independently builds and installs this example wheel, then
runs the example tests outside both source trees:

```sh
make tests/integration/provider-core \
  DOCKER_CONTEXT=science \
  WHEELHOUSE=/path/to/wheelhouse
```

That lane proves the public package boundary. It does not claim Jobs discovery,
interpretation, durable delivery, native-process supervision or remote execution.

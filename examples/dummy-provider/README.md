# Dummy provider example

This standalone Python project shows how scientific functions and a binding
adapter use the installed `provider_core` interface. The scientific package
contains only `science.py`, `binding.py` and its package marker.

```text
src/dummy_provider/
    science.py         scientific types, functions and state lifetimes
    binding.py         configured scientific call and lifecycle registrations
tests/
    test_science.py    direct calculations and scientific rejection/correction
    test_binding.py    binding declarations and installed package boundary
    test_execution.py one prepare/calculate path through the installed host API
```

The weighted-sum binding holds a reusable model and per-analysis state. Its
prepare call returns worker-local state that calculate consumes. The stateless
sum binding uses the same interface for a single calculation.

## Starting another provider

Copy this project into a new provider repository, rename the distribution and
import package, implement its scientific functions, and adapt the binding
factories. Test scientific behavior directly; test the production binding
through the execution backend that the provider actually uses.

Keep the shared allocation, dispatch, ownership and retirement rules tested in
`provider-core`; extend a provider's existing worker and service tests for its
real native protocol and resource behavior. No test-only scenario description
or interpreter is needed.

## Installed integration check

In this repository, the integration lane independently builds the candidate
`provider-core` and dummy-provider wheels, installs both outside their source
trees and runs the dummy provider's ordinary tests:

```sh
make tests/integration/provider-core \
  DOCKER_CONTEXT=science \
  WHEELHOUSE=/path/to/wheelhouse
```

The embedded-host test demonstrates the installed contribution/execution
interface; worker-process and deployed-service tests belong to their respective
backend and provider repositories.

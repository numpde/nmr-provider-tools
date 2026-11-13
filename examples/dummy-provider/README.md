# Dummy provider example

This standalone Python project shows how scientific functions and a binding
adapter use the installed `provider_core` interface. The scientific package
contains `science.py`, `binding.py`, `resources.toml` and its package marker.

```text
src/dummy_provider/
    science.py         scientific types, functions and state lifetimes
    binding.py         configured scientific call and lifecycle registrations
    resources.toml     CPU requirement shared by the three scientific calls
tests/
    test_science.py    direct calculations and scientific rejection/correction
    test_binding.py    binding declarations and installed package boundary
    test_execution.py one prepare/calculate path through the installed host API
```

The weighted-sum binding holds a reusable model and per-analysis state. Its
prepare call returns worker-local state that calculate consumes. The stateless
sum binding uses the same interface for a single calculation.

The configured frozen bindings expose typed members: `weighted.prepare`,
`weighted.calculate` and `summation.run`. Local execution uses
`analysis.submit_call(weighted.prepare, ...)`; the SDK derives the data-only
reference internally. The producer's `normalized` output connects to the consumer's
independently named `candidate` input through typed slots. Projection observes
the original producer operation; the backend owns live state admission,
consumption and cleanup. These named slot helpers are provider assembly fields;
the call's own slot declarations remain authoritative. See
[the typed API design](../../notes/085_typed_provider_api_end_state.txt).

Each binding reads the packaged `resources.toml` and validates it through the
SDK's resource constructor. The validated declaration is described as a JSON
asset in the binding revision. Changing a resource value changes that revision;
TOML whitespace and comments do not. Existing bindings retain their captured
requirements.

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
interface. It uses SDK `ResultConsumption` for preparation and calculation
reports, releasing each exact result only after its typed consumer succeeds.
Releasing the preparation report leaves its separate worker-local state available
to calculate. Worker-process and deployed-service tests belong to their respective
backend and provider repositories.

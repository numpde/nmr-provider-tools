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
    test_execution.py prepare/calculate and partial-loading cleanup through the host
```

The weighted-sum binding holds a reusable model and per-analysis state. Its
prepare call returns worker-local state that calculate consumes. The stateless
sum binding uses the same interface for a single calculation.

`WeightedSum` and `Sum` inherit the SDK's `Contribution` contract.
Their exported methods use keyword-only scientific arguments and read settings
from `self.configuration`. Provider assembly constructs the object and calls
`host.adopt(contribution=..., requirements=...)`. The host retains
that exact object before lazy initialization; the returned `HostedContribution`
contains its derived call inventory. Local execution uses
`analysis.submit_method(method=weighted.prepare, ...)`;
the SDK derives the data-only reference and encodes the typed input internally.
The producer's `normalized` output connects to the consumer's
independently named `candidate` input through typed slots. Projection observes
the original producer operation; the backend owns live state admission,
consumption and cleanup. These named slot helpers are provider assembly fields;
the call's own slot declarations remain authoritative. See
[the contribution interface](../../notes/086_configured_science_boundary.md).

The weighted initializer retains an empty model before loading it. Its installed
test makes loading fail after populating that model, checks that preparation never
runs, and proves retirement disposes the same partial model. Constructors only
capture settings and bookkeeping; `close` also supports retirement before use.

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


Provider constructor settings are ordinary object attributes. The SDK does not
serialize or hash them. Call identity refers to the adopted allocation; scientific
provenance remains provider-owned. Local submission uses the actual receiver
(e.g. `method=contribution.generate`). Adoption takes `requirements=...`;
hardware availability must be established by the environment running the provider.

The SDK supplies execution identities. Use allocation handles with
`host.open_analysis(allocations=frozenset({hosted.allocation}))`, retain returned operation
handles, and observe those handles to resume waiting. Each new submission means
new work. State kinds and ports derive their names from Python types and class
attributes rather than manually assigned labels.

Adoption records resource requirements and establishes scientific-object ownership.
It does not discover or allocate CPU/GPU hardware. The returned allocation handle
identifies an execution owner, not a hardware grant.

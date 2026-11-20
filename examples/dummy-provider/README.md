# Dummy provider example

This standalone Python project shows how scientific functions and a binding
adapter use the installed `provider_core` interface. Production startup selects
thread or persistent-process execution for the same scientific contribution.

```text
config/deployments/localhost/provider.toml   science, backend, capacity and policy
src/dummy_provider/
    science.py         scientific types, functions and state lifetimes
    binding.py         configured scientific call and lifecycle registrations
    payloads.py        JSON sample lists to scientific arrays, and back
    provider.py        deployment selection and SDK host lifetime
    process_factory.py installed contribution construction from startup values
tests/
    test_science.py    direct calculations and scientific rejection/correction
    test_binding.py    binding declarations and installed package boundary
    test_execution.py prepare/calculate and partial-loading cleanup through the host
    test_dummy_startup.py same request sequence on threads and persistent processes
```

The example imports `mock_numpy as np` and uses arrays for coefficient weights,
request samples and prepared samples. It checks arrays with
`np.isfinite(...).all()`, copies the arrays retained by a model or preparation,
and calculates through `np.dot` and `np.sum`. Retained numerical arrays and sum
accumulation use floating point to avoid integer wraparound. Scalar checks call `np.isfinite`
directly; there is no scalar-conversion or tuple-validation helper.

`payloads.py` owns the transport adapter: the SDK JSON codec admits JSON sample
lists, and the adapter converts them to numerical arrays. Its private transport
record uses the SDK's immutable sequence representation; scientific records and
retained state hold arrays throughout. The SDK has no numerical-library dependency.

The stand-in in `mocks/mock_numpy/` implements this exercised array subset and
has its own conformance tests. It uses independent storage, rejects mismatched
vectors and unsupported operations, and has no NumPy, Torch or SDK dependency.

The weighted-sum binding holds a reusable model and per-analysis state. Its
prepare call returns worker-local state that calculate consumes. The stateless
sum binding uses the same interface for a single calculation.

`WeightedSum` and `Sum` inherit the SDK's `Contribution` contract.
Their exported methods use keyword-only scientific arguments and read settings
from `self.configuration`. Provider assembly constructs the object and calls
`host.adopt(contribution=..., requirements=...)`. The host retains
that exact object before lazy initialization; the returned `HostedContribution`
contains its derived call inventory. Thread callers can also use
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
import package, implement its scientific functions, and construct your scientific
objects in the installed constructor. `open_provider()` reads the deployment
configuration and initializes its selected weighted model before yielding a
`Provider`. The provider exposes the hosted contribution, execution host and
operation policy. Callers open an analysis and use `hosted.call(...)` with SDK
`Submission.from_call(...)` for both backends. Keep the returned submission and
operation to resume observation; a new submission requests new scientific work.
For this toy example,
install `mocks/mock_numpy` first; a real provider replaces the stand-in with its
actual scientific dependency. Test scientific behavior directly; test the production binding
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

The installed startup test runs the same request sequence twice on each backend,
including scientific rejection and correction within one analysis. It checks
model or child reuse, the custom codec, state transfer, progress and retirement.
It uses SDK `ResultConsumption` for preparation and calculation
reports, releasing each exact result only after its typed consumer succeeds.
Releasing the preparation report leaves its separate worker-local state available
to calculate. Real scientific dependencies and deployed-service qualification
belong to their respective provider repositories.


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

`read_resource_requirements()` reads the external deployment document selected by
`PROVIDER_CONFIG_PATH` in `provider.py`. The configuration stays outside the wheel.

Set `execution.backend` to `thread` or `process` at startup. Process startup
requires an explicit deployment-owned `RunnerSession`; the SDK takes its ownership
when the process host is constructed. Earlier configuration refusals leave an
unconnected session with the caller. Thread startup rejects an unused process
session. The provider initializes its thread allocation explicitly; process
`start()` constructs and initializes the installed child before returning.

One process host owns one allocation, so startup selects the weighted offering.
`Sum` remains a separate stateless binding example. A native supervisor and its
hardware grant belong to deployment. Finish request analyses before leaving
`open_provider()`; the host then retires the scientific owner. No application
wrapper is needed for Dummy's direct prepare/calculate sequence.

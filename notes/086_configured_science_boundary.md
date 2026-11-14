# Configured scientific contributions

2026-11-14 — Implemented authoring boundary in `provider_core.scientific`.
This replaces the earlier six-parameter callback proposal in this note. The
execution ownership in 066 and 084 remains unchanged. This is not a release or
deployment claim.

## The configured object

A contribution **class** defines scientific behavior. Provider code constructs
an instance with immutable configuration and hands that exact object to the host.
The returned `HostedContribution` describes its exported calls and allocation;
it is not a recipe for constructing another instance. Independent allocations
need separate objects, which may share immutable configuration.

`Contribution[Config]` is a nominal hosting contract. Ordinary constructors
capture configuration and bookkeeping; they must not acquire resources.
Custom constructors call `super().__init__(configuration=...)`. Configuration
is fixed for the instance's lifetime. Optional `initialize` and
`close` methods acquire and release instance resources. Stateless contributions
inherit empty lifecycle methods and write only their calculations.

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class WeightedConfig:
    settings: science.WeightedSumSettings
    coefficients: science.CoefficientSet


class WeightedSum(Contribution[WeightedConfig]):
    @operation()
    def calculate(self, *, numbers: Numbers) -> Total:
        return science.weighted_sum(
            numbers=numbers,
            settings=self.configuration.settings,
            coefficients=self.configuration.coefficients,
        )
```

The illustration uses provider-defined scientific types/functions. The runnable
implementations are in `examples/`. `calculate` is a provider-selected name;
`@operation()` explicitly exports it. The decorator does not wrap direct calls.
Calling an instance method directly is ordinary Python, not managed execution.

## Handover and allocation

```python
host = EmbeddedHost(limits=host_limits)
weighted = WeightedSum(
    configuration=WeightedConfig(settings=settings, coefficients=coefficients),
)
hosted = host.adopt(
    contribution=weighted,
    allocation=ResourceAllocation(name="worker", grants=resource_grants),
    resources=call_requirements,
)
```

Adoption acquires no scientific resources. Component identity comes from the
contribution's module and qualified class name. Deployment owns installed-artifact
identity through its release and installation records; the call inventory does not
duplicate those records or accept a manually bumped implementation label.
Requirements describe scientific needs; allocation grants describe what deployment
actually supplies.

Validation precedes ownership transfer. Rejection leaves the object with its
caller, without initializing or closing it. Success retains the object and
permanently claims it for this allocation. Another host cannot adopt it, including
after retirement. A fresh allocation needs a fresh contribution.

The existing `Allocation(binding=...)` constructor remains for low-level
`ImplementationBinding` consumers; object contributions must use `adopt`.
Both paths use the same execution owners. Provider helpers such as
`create_binding()` are ordinary explicitly called assembly code, not SDK-discovered
entrypoints.

The registered inventory contains only effective, explicitly marked instance
methods. Normal Python overriding applies: an unmarked override removes an
inherited export. Aliased exports, async/generator methods and unsupported
class/static/property operation forms fail before handover.

## Who understands an operation

The provider's workflow understands scientific meaning and ordering. It chooses
an actual method and its input. The shared host understands declarations,
resources, permission, state ownership and execution; it invokes the method on
the selected instance after admission.

```python
analysis = host.open_analysis(
    analysis_id="example", allocation_names=("worker",),
)
reservation, = analysis.reservations
operation = analysis.submit_method(
    method=weighted.calculate,
    operation_id="calculate", scientific_input=numbers, states=(),
    reservation=reservation, limits=execution_limits,
    dispatch_guard=require_permission,
)
```

The reservation selects the adopted contribution's allocation, so submission does not
repeat its configuration. Both the exact receiver's bound method and its exported
class method are accepted; another receiver's bound method is rejected.
No scheduler infers that prepare must precede calculate from the method inventory.
`hosted.reference(method=weighted.calculate)`
projects the portable address for workflows using `Invocation` directly, as the
spectrum example does. Python scientific objects do not cross that transport
boundary. Typed methods compile to existing internal call records; there is no
second admission engine or generated method-proxy class.

Arguments after the receiver are keyword-only. Annotations identify one portable
input, an optional analysis session and optional control capability. Declared
state-input slots name exact live-state arguments. Ordinary methods return their
scientific result directly. Stateful producers retain `ScientificReturn` so the
existing host takes custody before validating outputs or encoding results.
When the method has no portable input argument, omit `scientific_input` at
submission; the SDK supplies its empty transport payload. Omitting a required
scientific input remains an error before operation admission.

## Lifetimes and failures

The host retains the contribution at adoption, initializes it lazily on its
allocation executor, and publishes readiness
only after success. Its original initialization failure remains observable.
Cleanup of a partially initialized instance uses that same owner; failed cleanup
retains the owner and its failure. Retirement also closes contributions that
were never initialized, so `close` must handle that state. Retirement waits for
dependent work and readers.

An initializer must retain an empty resource owner before a loader can fail:

```python
def initialize(self, *, assets: AssetAccess) -> None:
    self.model = science.create_empty_model(configuration=self.configuration)
    science.load_model(model=self.model, assets=assets)
```

The empty-owner constructor must not acquire resources. If it acquires and raises
before returning, only that constructor can clean up; the host never received
the missing owner. The dummy example tests partial loading and retirement of the
exact model. Process isolation does not change this ownership obligation.

`Analysis[ContributionType]` is a separate optional scientific owner, selected by
`Contribution.analysis_type`. The SDK retains it before its own initializer runs.
The dummy's `WeightedAnalysis` exists before preparation succeeds and survives a
rejected candidate. Per-analysis sessions are not shared contribution fields and
are not replaced by produced state objects.

Returned state belongs to its exact analysis and allocation. Guarded dispatch
controls consumption; releasing a result does not dispose separate prepared
state. Analysis close, allocation retirement, result release and durable delivery
remain distinct operations. Observation cancellation does not authorize replay
or establish that science stopped.

`AssetAccess` resolves verified deployment-owned snapshots during initialization.
`NoAssets` refuses file lookup. Content references may be fields of immutable
configuration. The resolver neither downloads data nor promises filesystem
confinement; snapshot lifetime and process/mount isolation remain deployment
responsibilities. The current examples keep their scientific coefficients and
reference records inline and need no file assets. No concrete retained snapshot
resolver is implemented here yet. A file-backed example needs that owner first;
hashing a path and reopening it later would not demonstrate verified acquisition.

## Identity and limits

The configured-v3 revision covers immutable configuration, its schema, the
contribution/session identities, exported input/result
schemas, state ports, resource requirements, concurrency, progress/interruption
and isolation declarations. Changing configuration or declared execution
requirements changes the revision. Code changes with unchanged class, configuration
and execution contracts may preserve it: this revision is not evidence of identical
installed code. Physical allocation identity is separate:
equal configured revisions never allow exchanging allocation-local state.

Legacy configured-v2 and sha256 references keep their original meaning. There is
no conversion, reattachment or replay implied by admitting the new scheme.

The SDK validates supported immutable configuration and uses it directly in each
embedded instance. It cannot prove that arbitrary provider methods avoid globals
or other ambient state. Deployment and provider tests still own those scientific
behavior guarantees. Custom codec behavior remains the provider's declared
format/code contract.

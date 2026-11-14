# Configured science and shared execution

2026-11-14 — Interface proposal, not an implemented API or a release decision.

This refines the Python hookup in 067/085. The lifecycle and execution ownership
in 066 and 084 remains authoritative. Current main is 492f22a; it accepts explicit
ConfiguredIdentity and zero-argument instance constructors. The signatures below
are proposed changes, not instructions that work against that release.

## Decision

Keep a complete **ImplementationBinding**, with typed configuration, named
scientific calls and explicit optional lifecycle hooks. One binding describes one
configured scientific implementation; one provider may expose several bindings.
Several physical instances may use the same binding.

The SDK supplies the binding's configuration to its callbacks. It derives the
portable description and configured revision internally. Provider authors do not
assemble ConfigurationDescription, AssetDescription or ConfiguredIdentity for
ordinary structured settings. A configuration-only generic wrapper is unnecessary.

Ordinary scientific functions and classes remain provider-owned. The SDK invokes
only explicitly supplied callbacks. A method named create_model, prepare or run
has no automatic SDK meaning. No compulsory provider base class, adapter factory,
decorator registry or method-name discovery is introduced.

## Owners and values

| Owner | Owns |
| --- | --- |
| Provider science | Scientific configuration/input/output types, calculations, validation, model loading and disposal, scientific retry decisions |
| Provider assembly | Which science implements which exported call; codecs, state use, scientific resource requirements and lifecycle callbacks |
| Deployment/acquisition | Installed code provenance, verified asset snapshots, file/device/network grants, backend placement and capacity |
| SDK configured binding | Immutable configuration value, callback declarations, derived description and revision |
| SDK execution host | Instance/analysis/operation ownership, reservations, dispatch permission, state custody, stop/cleanup observations and result retention |
| Service and delivery | Jobs/Attempts, scheduling, interpretation orchestration, report acceptance and publication |

A requirement such as one GPU is not a grant to use one. A configured revision is
not a worker identity, permission, or proof that a previous call can be replayed.

## Public Python shape

Retain `ImplementationBinding`: it connects one implementation to hosting.
`ProviderBinding` would suggest a whole provider service. `ContributionBinding`
is less precise about which configured unit can have several initialized copies.
Use `WeightedSumConfiguration` for the provider's complete scientific settings.
Keep `configuration` as the field name. These names identify responsibilities,
not lifecycle methods imposed on the scientific classes.

The essential proposed SDK signatures are:

```python
@dataclass(frozen=True, slots=True, kw_only=True)
class ImplementationBinding[ConfigT, InstanceT, AnalysisT]:
    binding_id: str
    implementation_revision: str
    configuration: ConfigT = field(repr=False)
    instance: InstanceHooks[ConfigT, InstanceT] | None
    analysis: AnalysisHooks[ConfigT, InstanceT, AnalysisT] | None
    resident_resources: tuple[ResourceRequirement, ...]
    isolation: IsolationRequirement
    # Named CallBinding fields are declared by provider subclasses.
    # description and configured revision are SDK-derived, init=False.

@dataclass(frozen=True, slots=True)
class InstanceHooks[ConfigT, InstanceT]:
    construct: Callable[[ConfigT, AssetAccess], InstanceT]
    initialize: Callable[[InstanceT], None]
    dispose: Callable[[InstanceT], None]

@dataclass(frozen=True, slots=True)
class AnalysisHooks[ConfigT, InstanceT, AnalysisT]:
    construct: Callable[[ConfigT, InstanceT, AssetAccess], AnalysisT]
    initialize: Callable[[AnalysisT], None]
    dispose: Callable[[AnalysisT], None]

@dataclass(frozen=True, slots=True)
class CallContext[ConfigT, InstanceT, AnalysisT, InputT, StateT]:
    configuration: ConfigT
    instance: InstanceT
    analysis: AnalysisT
    assets: AssetAccess
    scientific_input: InputT
    state: StateT
    control: ScientificControl | None
```

These are contract excerpts, omitting SDK validation and derived fields. Every
callback and scientific field has the same meaning as the existing interfaces.
The changes are explicit configuration delivery and internal metadata derivation.
The target call declaration carries the full callback type:

```python
class CallBinding[ConfigT, InstanceT, AnalysisT, InputT, StateT, OutputT]:
    scientific_call: Callable[
        [CallContext[ConfigT, InstanceT, AnalysisT, InputT, StateT]],
        ScientificReturn[OutputT],
    ]
    # Existing typed codecs, state slots and execution declarations remain.
```

Use provider-local type aliases to avoid repeating the three owner types on
every field. Runtime assembly also checks each declared member against its
binding's actual configuration and hooks: subclass authors can misdeclare a
field's generic owner types, and annotations alone cannot prohibit arbitrary
inconsistent subclass declarations. Static fixtures must reject ordinary
callback/codec/hook mismatches; runtime tests cover the assembly invariant.

Use `None` configuration for unconfigured science. Instance and analysis hooks
are independently optional. Absent hooks REQUIRE the corresponding type argument
to be None: a configured stateless binding is ImplementationBinding[C, None, None],
and its callbacks use CallContext[C, None, None, Input, NoState]. A binding that
declares a concrete instance/analysis type but supplies no matching hooks is
rejected. Optionality never silently makes a concrete type nullable.
An analysis constructor may therefore accept an explicitly None-typed instance. The plain unary
function constructor remains available for calls with no configured dependencies,
live state or control; no fake model/session is required.

## Complete weighted-science hookup

The provider owns the types and functions below. The SDK does not discover any
of their names:

```python
@dataclass(frozen=True, slots=True)
class WeightedSumConfiguration:
    settings: science.WeightedSumSettings
    coefficients: science.CoefficientSet


def construct_model(
    config: WeightedSumConfiguration, assets: AssetAccess,
) -> science.WeightedSumModel:
    return science.create_model(
        settings=config.settings,
        coefficients=config.coefficients,
    )


def construct_analysis(
    config: WeightedSumConfiguration,
    model: science.WeightedSumModel,
    assets: AssetAccess,
) -> science.WeightedSumAnalysis:
    return science.create_analysis(model)


def prepare(
    ctx: CallContext[
        WeightedSumConfiguration, science.WeightedSumModel,
        science.WeightedSumAnalysis, science.PreparationInput, NoState,
    ],
) -> ScientificReturn[science.PreparationResult]:
    value = science.prepare(analysis=ctx.analysis, candidate=ctx.scientific_input)
    return ScientificReturn(
        value=science.PreparationResult(count=value.value_count),
        states=(prepared_output.returned(value),),
    )
```

`prepared_output` is the existing typed StateOutputSlot, constructed once in
provider assembly and also supplied to the prepare CallBinding. The calculate
adapter receives its typed CalculationState and calls the existing science.
The provider declares its named call fields as today:

```python
type WeightedCall[InputT, StateT, OutputT] = CallBinding[
    WeightedSumConfiguration, science.WeightedSumModel, science.WeightedSumAnalysis,
    InputT, StateT, OutputT,
]


@dataclass(frozen=True, slots=True, kw_only=True)
class WeightedSumBinding(ImplementationBinding[
    WeightedSumConfiguration, science.WeightedSumModel, science.WeightedSumAnalysis,
]):
    prepare: WeightedCall[science.PreparationInput, NoState, science.PreparationResult]
    calculate: WeightedCall[NoInput, science.CalculationState, science.PredictionResult]
```

The two declarations below connect actual callbacks to codecs, state ports and
requirements. `call_resources` is loaded from the provider's maintained resource
configuration, not inferred from settings or granted by constructing the binding.
The names `prepare` and `calculate` are stable exported operation names; local
callers use the typed fields instead of repeating those strings.

```python
prepared_kind = StateKind("weighted.prepared.v1", science.PreparedValues)
prepared_output = prepared_kind.output(name="normalized", dispose=science.dispose_prepared)
prepared_input = prepared_kind.input(name="candidate", use=StateUse.CONSUME)
calculation_state = SlotStateBinding(
    value_type=science.CalculationState,
    inputs=(prepared_input,),
    assemble=lambda values: science.CalculationState(prepared=prepared_input.read(values)),
)


def calculate(
    ctx: CallContext[
        WeightedSumConfiguration, science.WeightedSumModel,
        science.WeightedSumAnalysis, NoInput, science.CalculationState,
    ],
) -> ScientificReturn[science.PredictionResult]:
    return ScientificReturn(value=science.calculate(analysis=ctx.analysis, state=ctx.state))


prepare_call = CallBinding.from_json_dataclasses(
    name="prepare",
    input_type=science.PreparationInput, state_type=NoState,
    result_type=science.PreparationResult,
    input_format="weighted.preparation-input.v1",
    result_format="weighted.preparation-result.v1",
    state_outputs=(prepared_output,), resources=call_resources,
    concurrency=ConcurrencyRequirement.INSTANCE_SERIAL,
    interruption=InterruptionSupport.NONE, progress=False,
    scientific_call=prepare,
)
calculate_call = CallBinding.from_json_dataclasses(
    name="calculate",
    input_type=NoInput, state_type=science.CalculationState,
    result_type=science.PredictionResult,
    result_format="weighted.prediction-result.v1",
    state_binding=calculation_state, state_outputs=(), resources=call_resources,
    concurrency=ConcurrencyRequirement.INSTANCE_SERIAL,
    interruption=InterruptionSupport.NONE, progress=False,
    scientific_call=calculate,
)
```

Finally, assembly supplies deployment's recorded `installed_revision` and the
actual scientific values. There is no separately supplied identity object:

```python
binding = WeightedSumBinding(
    binding_id="weighted-sum",
    implementation_revision=installed_revision,
    configuration=WeightedSumConfiguration(settings=settings, coefficients=coefficients),
    prepare=prepare_call,
    calculate=calculate_call,
    instance=InstanceHooks(
        construct=construct_model,
        initialize=science.load_model,
        dispose=science.dispose_model,
    ),
    analysis=AnalysisHooks(
        construct=construct_analysis,
        initialize=science.initialize_analysis,
        dispose=science.dispose_analysis,
    ),
    resident_resources=(),
    isolation=IsolationRequirement.SHARED_PROCESS,
)
```

The SDK's instance startup actually calls:

```python
instance = binding.instance.construct(binding.configuration, allocation.asset_access)
# Host retains instance and its disposer before allowing initialization effects.
binding.instance.initialize(instance)
```

Analysis startup likewise passes `(binding.configuration, instance, allocation.asset_access)` and retains
the returned owner before initialization. Every advanced scientific invocation
receives a CallContext whose configuration is `binding.configuration`. For a
configured stateless baseline subtraction, the adapter directly reads
`ctx.configuration.baseline`; instance and analysis hooks are both None.

Inside an embedded host these are the same immutable Python object references.
Across a worker process, deployment reconstructs the typed configuration using
its declared scientific code and the SDK's versioned configuration document.
It must reproduce the selected configured revision before readiness. There is
no promise of preserving Python object identity across processes, nor of loading
arbitrary classes, import paths or pickles supplied by a job.

## From binding to actual work

Host composition remains explicit. `asset_access` is a proposed new Allocation
input. `deployment_assets` implements AssetAccess over the deployment's retained
verified snapshots; for this all-inline example it is an empty resolver. The
snapshot leases are retained by the deployment/acquisition owner until dependent
allocations retire and their readers settle. Deployment must supply a live
resolver and preserve that lifetime; the path-returning protocol alone cannot
prove lease ownership. Premature lease loss propagates its original acquisition
failure and must never trigger substitution of different content:

```python
allocation = Allocation(
    name="weighted-1", binding=binding, grants=grants,
    asset_access=deployment_assets,
)
host = EmbeddedHost((allocation,), limits=host_limits)
analysis = host.open_analysis("analysis-1", allocation_names=("weighted-1",))
reservation, = analysis.reservations
operation = analysis.submit_call(
    binding.prepare,
    operation_id="prepare-1",
    payload=binding.prepare.encode_input(candidate),
    states=(),
    reservation=reservation,
    limits=execution_limits,
    dispatch_guard=require_permission,
)
```

submit_call projects the named member into the existing data-only Invocation.
The scheduler/remote execution boundary carries CallRef, bounded encoded input,
state references, exact reservation, operation ID and limits. The local guard is
separate from serializable data. No callbacks, configuration objects or framework
models cross that execution boundary. The SDK need not deserialize a binding to
execute a request: the worker already hosts a deployed registration.

Scientific input/output format IDs remain explicit contracts: independent calls
and backends exchange these payloads. Configuration encoding is a different
concern, owned and versioned once by the SDK, with no per-provider JSON format ID.

## Configuration, assets and identity

Configuration contains settings fixed for that registration. Per-job candidates,
corrections, sampling choices and per-request seeds belong to declared call input
or analysis state when they vary during work; they do not mutate the binding or
require a new registration on every call.

Configuration is a frozen portable value record, recursively containing supported
immutable scalars/records/tuples and explicitly typed asset references. Reject
unsupported values rather than converting repr(), pickle, arbitrary objects or
closures. Serialization is deterministic and performed once at binding assembly;
it is not a user-authored metadata-building pipeline. Keep the current strict
codec's validation, and version any change in its bytes or meaning.

Small scientific data such as dummy coefficients can be part of that value.
Large model files remain files. Acquisition owns a verified immutable snapshot
and its content identity/size. The configuration refers to that content. Local
mount paths, open streams and mutable model/tensor objects are runtime access,
not scientific configuration identity. A pathname hash does not identify its
contents; a read-only mount alone does not establish an immutable source file.

Deployment binds typed asset references to granted worker-local access. The
proposed narrow host-local capability is:

```python
class AssetAccess(Protocol):
    def path(self, reference: AssetRef) -> Path:
        """Return the already verified snapshot's granted local path."""
```

AssetRef names content (digest, size and declared content kind), not a path or a
string lookup key. Admission binds the configured references to the deployment's
maintained acquisition owners before model initialization. The access object is
allocation-scoped, resolves only those references through this API and performs
no implicit search, download or new grant. path() resolves an already acquired snapshot; scientific
initialization, not bookkeeping construction, opens/loads it. A configuration
with missing or mismatched asset grants is refused before scientific effects.
An allocation with no external assets has an empty access capability. The SDK
passes this capability alongside configuration to constructors, and in CallContext
for configured lazy scientific reads. A returned Path is an API-level resolver
result, not an OS capability: ordinary Python can derive neighboring paths or
use ambient filesystem APIs. Actual filesystem confinement requires deployment
mount/process isolation; trusted embedded callbacks have no SDK sandbox promise.
The interface exposes no Jobs, scheduler or administrative methods. Direct
unconfigured science can ignore it.

A file-backed model adapter has the following ownership order (the scientific
runtime names are illustrative; actual providers retain their maintained loaders):

```python
def construct(config: ModelConfiguration, assets: AssetAccess) -> ModelOwner:
    return ModelOwner(config=config, assets=assets)  # bookkeeping only


def initialize(owner: ModelOwner) -> None:
    path = owner.assets.path(owner.config.checkpoint)
    owner.initialize_from_checkpoint(path)


def dispose(owner: ModelOwner) -> None:
    owner.close()
```

The ModelOwner already exists under host custody before any file/model loading.
Its initializer must retain partial resources as it acquires them; a library that
cannot expose partial cleanup needs supervised process retirement instead of an
embedded safety claim. Assigning a completed model only after an opaque loader
returns is not sufficient proof of partial-failure cleanup.

Snapshot acquisition owners outlive all dependent allocations and original readers.
Each instance separately owns its loaded model/mappings until its disposer and
readers settle. Cancellation of a waiter releases neither. Config values do not
close files. AssetAccess is a reusable reference-scoped capability, not a shared
open stream. For scientific APIs requiring a directory/tree, acquisition must
supply its existing verified manifest-backed snapshot; an unverified directory
path cannot be promoted into an AssetRef. Real provider qualification must prove
this adapter with its actual loader before claiming binary-asset support.

The SDK derives the configured registration revision from the declared installed
implementation revision, canonical configuration including asset identities,
and the derived public call contract: call names, payload formats, state kinds/use,
progress/interruption/concurrency capabilities, per-call and resident resource
requirements, minimum isolation and SDK contract version. These requirements
are part of the immutable registration contract. Changing them changes that
registration's revision even if numerical outputs happen to be equivalent.
The manifest encoding has an explicit SDK version. Actual resource grants,
placement, credentials, local asset paths and allocation IDs are excluded.
Public advertisements expose the description/digest, not configuration contents;
configuration transfer belongs to authorized deployment/worker initialization.

A configured revision means the same declared code/configuration/contract across
workers. An allocation reference identifies a particular live instance and boot.
Both are required by the 066 target: HF workers with the same checkpoint may be
compatible candidates, but cannot exchange worker-local prepared state. A queued
request for HF must not silently become CHF or switch coefficients after a
replacement. Fingerprints do not establish deterministic numerical results,
authorize replay, or prove that arbitrary callback code obeys its declarations.
The installed code revision is a deployment assertion whose provenance must be
checked at installation, not discovered by hashing Python functions.

## Lifecycle and failure contract

- Construction creates only bookkeeping; initialization may acquire resources.
  Retain the object and disposer before initialize. Partial failures keep cleanup
  ownership and report the original cause.
- Permission is checked at actual managed dispatch after waits, not only at
  registration. A denied consumer leaves its prepared state usable.
- States stay attached to exact host, allocation, analysis and producer operation.
  Independent output/input names, kinds and BORROW/CONSUME uses remain explicit.
  Adopt all returned objects before type validation or result encoding can fail.
- Cancellation ends a wait, not the original operation. Stop and retirement have
  observable outcomes. No uncertainty, codec failure or cleanup failure creates
  permission to repeat science.
- Scientific input rejection remains distinct from operator/runtime failure.
  Preserve original causes; logging redaction occurs at the logging boundary.
- Analysis cleanup, instance retirement, result release, durable delivery
  acceptance and API publication remain different owned actions.

## Acceptance and migration

1. Stateless sum: no configuration/model/session machinery; direct and hosted
   numerical outputs agree. Configured baseline receives its configuration through
   CallContext with no fabricated model lifetime.
2. Reference matching: two sequential analyses share one loaded index. Two
   allocations of the same configuration have distinct live state ownership.
3. Weighted science: instance and analysis constructors receive the exact stored
   configuration object; output `normalized` can feed input `candidate`.
4. Queue selection: A requested with B's reservation refuses before effects;
   A remains usable alongside B; retiring A never silently reroutes to B.
5. Equal configured revisions do not admit foreign allocation/analysis states;
   changed code, scientific settings, content or call contract changes revision.
6. Partial initialization, guard withdrawal, encoding failure, cancelled waits
   and ambiguous dispatch retain original owners and causes without replay.
7. Worker reconstruction reproduces the selected configuration/revision; changed
   asset content fails verification before use, even at the same pathname.
8. A real static checker proves the supported generic links and rejects mismatched
   configuration/constructor/context/codec types. Runtime checks and isolated
   installed examples prove complementary behavior, not static correctness.

Implement the new configuration owner, hook/context delivery and metadata removal
as one coherent SDK/examples change. Remove the displaced declaration helpers and
provider metadata assembly; do not retain parallel input authorities. Keep call
names/resource rules/lifecycle safety rules. A changed manifest must use a new
explicit revision scheme, preserving already-running owners under their original
meaning. Do not reinterpret configured-v2 hashes or reattach old state to new work.

Then connect real provider paths under one qualified SDK release, preserving their
scientific policy and deployment authority. Existing shim boundaries may be used
only with named deletion work. Demonstrations do not count as provider adoption.
Publication, repins and deployments are separate work; none is performed here.

## Alternatives rejected

- A configuration-only ContributionBinding: it does not connect science to SDK
  execution and gives a data container an operational name.
- Automatic discovery of create_model/run methods: adds implicit lifecycle and
  export rules; arbitrary object methods do not specify codecs or state use.
- Mandatory SetupSnapshot + JsonConfiguration + adapter factory + register/bind
  stages: additional public owners and assembly stages for one immutable value.
- Provider-supplied JSON descriptions/hashes alongside captured configuration:
  leaves two declarations that can drift and exposes encoding bookkeeping.
- Fingerprints as a substitute for instance/operation identity: violates live
  ownership even when two workers run scientifically equivalent configurations.

This is a design decision, not a claim of implemented generic correctness or
complete remote execution. Validate the concrete interfaces against real provider
paths and static fixtures before freezing a release contract.

## Review disposition

All four provider owners supplied model/state/lifecycle requirements and concrete
failure scenarios. Two standing auditors reviewed the integrated proposal.
Accepted corrections: complete callback generics, explicit None types for absent
lifecycles, explicit asset resolver injection at Allocation/constructor/context,
full declared-contract manifest coverage, and accurate filesystem confinement
limits. The snapshot/adapter/register pipeline alternative was rejected because
it adds public assembly owners without removing the need for explicit callbacks.
This review is design/source evidence only; none of the proposed interfaces has
been implemented or qualified by this note.

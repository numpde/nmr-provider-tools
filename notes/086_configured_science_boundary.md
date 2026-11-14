# Scientific objects and execution ownership

Provider code constructs ordinary scientific objects and hands each exact object
to the SDK. Settings, model selection and scientific provenance belong to the
provider. Contribution has no configuration type parameter or settings storage.

```python
class Peaks(sdk.Contribution):
    def __init__(self, *, settings):
        super().__init__()
        self.settings = settings

    @sdk.operation()
    def run(self, *, spectrum: Spectrum) -> PeakReport:
        return detect_peaks(spectrum=spectrum, settings=self.settings)

peaks = Peaks(settings=settings)
hosted = host.adopt(
    contribution=peaks,
    requirements=requirements,
)
```

Constructor arguments are ordinary provider-owned values. The SDK does not
serialize, freeze or hash them. Constructors retain bookkeeping; initialize
acquires resources after adoption establishes custody. Close handles never-used
and partially initialized objects. Adoption rejects duplicate ownership, even
across hosts and after retirement. Rejected adoption leaves the caller owning
the object. A fresh allocation requires a fresh object.

Local execution submits a bound method:

```python
analysis.submit_method(
    method=peaks.run, scientific_input=spectrum,
    states=(), reservation=reservation, limits=limits,
    dispatch_guard=require_permission,
)
```

The SDK compares the bound receiver with the object retained by the reservation,
using Python identity. Class methods cannot identify the receiver at submission.
The host also checks explicit exports, capacity, permission and state ownership.

HostedContribution supplies derived call codecs and portable references. Its
binding identity encodes the existing host and allocation identity. Its revision
summarizes the declared execution contract and schemas, not scientific settings,
model provenance or code bytes. Portable calls from another allocation are
refused even when class, settings and execution contracts match. The existing
legacy ImplementationBinding path keeps its original reference semantics.

Requirements describe the resource conditions requested for execution. Adoption
records them; it does not discover or reserve CPU/GPU hardware. Resident and
operation requirements retain distinct lifetimes. Invocation permission comes
from the execution reservation and dispatch guard, independently of hardware.

Analysis sessions and produced state retain their existing ownership. State is
allocation-local, consumed only after guarded dispatch. Operation observation,
analysis cleanup, result release and host retirement remain separate. Cancelled
waits never authorize scientific replay.

The NMRPeak mock uses the real SDK interpreter and scientific host. Its checkpoint
loader follows NMRPeak's same-descriptor verification and loading pattern.
Deployment retains checkpoint files during use. Scientific provenance remains
in provider-owned result records. No new snapshot service is required.

The SDK creates allocation, analysis and operation identities. Provider code
passes handles rather than choosing names:

```python
analysis = host.open_analysis(allocations=(hosted.allocation,))
reservation = analysis.reservation(allocation=hosted.allocation)
```

Each local submission creates a distinct operation. Retain its returned handle
to observe it again; resubmitting the method requests new work. Portable Invocation
records likewise receive generated identities and can be retained for transport
resubmission. Explicit IDs remain a low-level protocol facility, not an authoring
requirement.

StateKind(value_type=PreparedInput) derives its identity from the scientific
Python type. Declare ports as class attributes, such as
candidate = kind.input(use=StateUse.CONSUME). Python supplies the port name;
that attribute matches the method's candidate parameter. Inherited ports retain
their names; one inferred port cannot be declared under two different names.

Host ownership follows outstanding obligations rather than permanent history.
A successful analysis close clears its operation/state indexes and removes the
analysis from the host. Available results remain owned and readable through
operation handles until released. Running work, undisposed scientific values and
failed cleanup remain retained. Input-byte reservations end when execution
finishes; result-byte reservations end when the result is released.

Settled local operations leave both indexes once their result/value/state
obligations settle; their handles remain usable for observation.

Portable submission deduplication is scoped to an open analysis. That analysis
retains its accepted request history until close; closed analyses reject every
submission, including a previously accepted request. There is no host-wide
completed-request archive. EmbeddedHost therefore has no analysis_records or
operation_records parameters: resident reservations bound concurrent analyses, and
input/result/state capacity bounds admitted scientific work.

EmbeddedHost declares the six capacity/time parameters directly as typed,
keyword-only constructor arguments. Provider startup can pass
**provider_config["execution"]["host"] without an intermediate policy record. The host
validates those values at construction and has no knowledge of TOML.

Modern adoption accepts a contribution and ResourceRequirement records directly;
asset access is optional. No caller-created ResourceAllocation or capacity
assertion is needed. The returned allocation reference identifies the hosted
object and its executor, not a hardware grant. ResourceRequirement is the single
resource declaration type, including on the older ImplementationBinding path.
Requirement collections are frozensets: all declared needs must be met together,
with no ordering or repeated identical records. Conflicting records for one
resource identity are rejected. Exported arrays use a canonical order so hash
iteration cannot change an execution-contract revision.
Neither path discovers, reserves or validates hardware availability. Backend
execution support is checked independently before ownership is accepted.

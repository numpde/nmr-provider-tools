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
    allocation=ResourceAllocation(name="peaks", grants=grants),
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
    method=peaks.run, operation_id="peaks", scientific_input=spectrum,
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

Requirements describe what execution needs; grants describe deployment's
assignment. Resident requirements and operation requirements retain their
existing distinct lifetimes. Neither declarations nor Python identity grant
execution authority; the reservation and dispatch guard do that.

Analysis sessions and produced state retain their existing ownership. State is
allocation-local, consumed only after guarded dispatch. Operation observation,
analysis cleanup, result release and host retirement remain separate. Cancelled
waits never authorize scientific replay.

The NMRPeak mock uses the real SDK interpreter and scientific host. Its checkpoint
loader follows NMRPeak's same-descriptor verification and loading pattern.
Deployment retains checkpoint files during use. Scientific provenance remains
in provider-owned result records. No new snapshot service is required.

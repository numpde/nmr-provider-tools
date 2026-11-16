# Scientific execution across threads and processes

## Purpose

Allow a provider to use the same scientific contribution through either an
in-process worker thread or a separately supervised process. The provider should
choose the execution boundary that fits the model without rewriting its request
workflow around the backend's private objects.

This note is a design proposal. It does not establish that a process-backed
host, a portable constructor protocol, or automatic model recovery exists.
Note [086](086_configured_science_boundary.md) describes the current in-process
contract; where it requires handing over an exact Python object and submitting
its bound methods, the process-capable design below would change that contract.

## What exists today

`InProcessExecutionHost` adopts a contribution object created by provider code.
It retains that exact object, initializes it on a dedicated worker thread, runs
its scientific methods there, and calls its cleanup method on the same worker.
The host can initialize an allocation before requests arrive and can accept a
fresh contribution after other analyses have started. Successfully retired
allocations leave the host inventory; pending and failed cleanup remain owned.

The NMRPeak mock waits for both configured models to load before yielding its
offerings. A loading failure stops startup and the host joins cleanup; the
example does not yet retry failed models or advertise partial availability.

The request workflow currently obtains methods and prompt selection through
`hosted_predictor.contribution`. That is a direct reference to a live Python
object. It works in this process, but a parent process cannot use it to access
an object owned by a child. The workflow also names the concrete in-process
analysis executor. These are current implementation facts, not proof that a
process host already has the same interface.

## Proposed boundary

The provider supplies its scientific class, provider-written construction code,
startup data, and resource requirements. The selected SDK host constructs,
initializes, executes and closes the contribution within its own execution
boundary. The provider receives a handle for submitting its declared operations,
not a reference to the live scientific object.

The following illustrates the intended authoring experience. These names and
signatures are proposals, not APIs available in the current SDK:

```python
# Provider code. PeakStartup is provider-owned startup data.
def create_hf(*, startup: PeakStartup) -> Hf:
    return Hf(
        configuration=startup.configuration,
        checkpoint_path=startup.checkpoint_path,
        create_model=Model,
    )


# Provider startup. The host owns the contribution it creates.
hosted_hf = await host.start(
    constructor=create_hf,
    startup=hf_startup,
    requirements=requirements,
)

# Request assembly. These are Python method declarations, not provider-chosen IDs.
preparation_call = hosted_hf.call(method=Hf.prepare)
generation_call = hosted_hf.call(method=Hf.generate)
```

`Hf.prepare` and `Hf.generate` denote declarations on the scientific class;
the hosted handle identifies which hosted instance receives the call. The SDK
must validate that the method belongs to that handle's declared contribution.
The provider should not have to write a separate string name for either call.
The process backend may need its own internal protocol identifiers, but those
are not a second provider-authored operation inventory.

Provider-owned prompt choices belong on the offering or its request assembly,
not on a live scientific object that the provider must inspect across a process
boundary. The workflow should depend on the execution operations it actually
uses, not `InProcessAnalysisExecutor`. The existing `AnalysisExecution`
protocol covers submission, stopping and closing, but the complete interface
for acquiring a reservation and binding a method still needs to be designed.

## Construction and state

For a thread host, provider construction code can run in the same process. For
a process host, the contribution and its model must be created in the child.
The provider-written constructor must therefore be available in the worker's
installed code, and its startup data must have an agreed representation. An
arbitrary closure, live file handle, GPU object or mutable parent-process object
cannot be assumed to cross that boundary.

The process bootstrap, code installation and version agreement are part of the
backend contract. Passing a Python callable in the example above does not itself
solve how a child resolves it. We must prove the selected mechanism on the
supported deployment platforms rather than assume that serializing a callable
or importing the same name always reproduces the same code.

Prepared scientific state remains with the contribution that created it. The
provider holds a state handle, not the live prepared object. Inputs and results
that cross the process boundary need supported encodings and size limits; state
handles must be checked against the live allocation, analysis and producer.
The process backend must preserve the declared borrow/consume rules and must
not turn an uncertain operation into permission to replay it.

## Failure, readiness and recovery

An offering is available only after its model has loaded and the host has
reported successful initialization. Loading a model does not prove that a full
prediction will succeed. The provider decides whether a failed offering should
be retried and whether independent offerings can remain available. The SDK owns
the original execution and cleanup mechanics; it does not infer retry policy
from an exception or claim that resource requirements allocate GPU capacity.

Cancellation or a timeout while waiting for a thread does not stop that thread.
The host retains the original load and joins it before closing its object. A
process host may be able to stop the child, but must report whether the process
actually exited and whether dependent work and cleanup settled. A stop request,
deadline or lost response alone is not proof that an operation did not run.

When a model is withdrawn, new requests must no longer select it. The owner
must decide how existing requests finish or fail before retirement; current
allocation retirement closes its analysis and is not automatically a graceful
drain. A replacement uses a fresh contribution and allocation only after the
prior owner is settled or its unresolved state is explicitly handled. Other
offerings should remain usable when they do not depend on the failed owner.

In-process execution can propagate an original Python exception object. Across
a process boundary, object identity and arbitrary exception types cannot be
promised. Preserve the available type, message, phase and causal evidence;
distinguish a reported scientific failure from a child that exited or stopped
responding without reporting one. Do not replace operational errors with a
generic user error or silently turn uncertain work into a retry.

## How to qualify the boundary

Do not declare the design portable merely because a protocol or fake backend
type-checks. Implement a real process-backed vertical slice and run the same
provider workflow against both hosts. Include at least:

- Successful initialization, preparation, generation, result delivery and
  cleanup, using the actual provider-written scientific contribution.
- Failed and partial initialization, cancellation during loading, a loading
  deadline, and independent cleanup failure, with the original evidence kept.
- A child killed before a call starts, during scientific work and after work
  may have completed but before its result is observed. Check that no case
  invents a result or automatically repeats uncertain work.
- Prepared-state ownership, single-use consumption, stale handles, concurrent
  requests and cleanup while work is still running.
- Replacement with a fresh contribution while another offering remains usable;
  failed or unfinished cleanup must not be reported as successful retirement.
- Provider startup data, worker code/version agreement, asset access and
  communication failures under the actual deployment's isolation and identity.

Keep thread-host tests as well: the two backends have different failure modes,
even when they expose the same provider-facing workflow. Actual real-provider
and GPU qualification remains separate from a mock example's passing tests.

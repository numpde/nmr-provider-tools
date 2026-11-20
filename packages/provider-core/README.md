# Provider core

Model-independent signed NMR API operations, protocol admission, durable result
delivery and reusable lifecycle helpers. Providers own scientific preparation,
inference, offerings, model configuration and their execution policy.

## Contents

- [Scientific contributions](#scientific-contributions)
- [Scientific process hosting](#scientific-process-hosting)
- [Type requirements](#type-requirements)
- [Scientific input tools](#scientific-input-tools)
- [Attempts and capacity](#attempts-and-capacity)
- [Crashes and recovery](#crashes-and-recovery)
- [Shared result delivery](#shared-result-delivery)
- [Runner lifecycle](#runner-lifecycle)
- [Conversations and tools](#conversations-and-tools)
- [Shared interpretation](#shared-interpretation)
- [API behavior](#api-behavior)
- [Package and qualification](#package-and-qualification)
- [Source history](#source-history)


## Scientific contributions

`provider_core.scientific.Contribution` is the base for scientific objects whose
lifetime the SDK manages. Providers construct their objects with ordinary Python
arguments and implement keyword-only methods marked with `@operation()`.
Optional `initialize(assets=...)` and `close()` methods own loaded instance
resources. An optional `Analysis[ContributionType]` owns a separate scientific
session for each analysis.

With `InProcessExecutionHost`, provider setup passes each exact object to
`host.adopt(contribution=..., requirements=...)`. The returned hosted handle
exposes its compiled methods and allocation. Workflows can submit actual bound
methods through
`InProcessAnalysisExecutor.submit_method(method=..., scientific_input=..., reservation=...,
policy=...)`. Portable `Invocation` callers obtain addresses with
`hosted.reference(method=...)`. For a workflow shared with the process backend,
select exported class methods with `hosted.call(method=ContributionType.prepare)`
and pass those bindings to `Submission` or `PreparationTool`. See
[scientific process hosting](#scientific-process-hosting) for child construction,
deployment requirements and the current supported scope.

Configured-v3 revisions summarize the declared execution contract and input/result
schemas. Settings, model provenance and installed code identity remain provider
and deployment responsibilities. The SDK owns serialization and lifecycle;
providers own scientific meaning and operation ordering. See the
[implemented interface](../../notes/086_configured_science_boundary.md) and the
[standalone examples](../../examples/README--to-be-revised-after-process-refac.md). This authoring API adds no
automatic retries, worker pool, Jobs service or deployment authority.

After `observe_completion(operation=operation)`, use
`observation.require_result()` to obtain the usable result handle or raise the
original failure causes. Successful science alone does not prove result encoding
succeeded. Workflows can still inspect the observation directly when deciding
whether to repair input or proceed to another scientific step.

Own report acceptance inside its resource lifetime:

```python
async with ResultConsumption(
    operation=operation,
    result=observation.require_result(),
    receive=receive_report,
) as consumption:
    await consumption.consume()
```

The context joins an already started receiver before outer cleanup and preserves
independent body and settlement failures. Entering does not start delivery, and
repeated waits never resend. The provider still owns pipeline ordering, execution
stop decisions and the lifetime of the receiver's external resources.

`InProcessExecutionHost` is also an async context manager. Put request tasks and receiver
contexts inside the host lifetime. Its exit requests SDK retirement and joins the
original cleanup, deferring cancellation until cleanup completes or reports a
failure. A failure does not establish that every resource retired. The host does
not join arbitrary caller-owned tasks or receivers.

Use `observe_cleanup(control=...)` for an interruptible observation of retained
cleanup. Use `settle_cleanup(control=...)` when shutdown must finish observing that
cleanup before outer resources can close. Settlement can wait indefinitely;
cleanup's overdue flag does not cancel a scientific thread or end the join. Both
functions observe an existing control without repeating its cleanup request.

## Scientific process hosting

`provider_core.process_host.ProcessExecutionHost` owns one persistent scientific
child through a deployment-supplied `RunnerSession`. The current profile uses
the existing Linux/native container runtime, with parent and child using the
same pinned installed SDK and provider code. One host accepts one allocation
and one active analysis at a time; successfully closed analyses can be followed
by new analyses on the same loaded model. This is an execution boundary within
the existing containment, not an additional security sandbox.

**Qualification remains outstanding on `feature/process-scientific-host-20261119`.**
At the 20 November 2026 grading checkpoint, Docker was unavailable and no
installed/native or integration run established this branch's runtime behavior.
Use the maintained qualification commands, independent grades and evidence
requirements in [Note 094](../../notes/094_process_boundary_grading_20261120.txt).
Source review and authored tests do not establish real-model/GPU suitability,
cross-version compatibility, additional platforms or unattended recovery.

### Deployment and startup

Deployment starts the existing `provider_core.runner` with
`--owner-session-lifetime` and a fixed installed worker command after `--`:

```sh
python -I -B -m provider_core.process_worker
```

The runner still requires its role, socket, scratch and status-directory paths
and explicit startup, TERM, KILL and cleanup budgets. See
[Runner lifecycle](#runner-lifecycle). Deployment provisions the private
directories, verifies the stable lifetime lock identity, and supplies a fresh,
unconnected `RunnerSession(path, role=..., expected_lock_identity=...,
startup_seconds=..., retirement_seconds=...)`. The expected identity is the
lock's `(st_dev, st_ino)` pair. `ProcessExecutionHost` uses that session; it does
not launch or discover a supervisor.

The deployment must grant only the environment, files and descriptors needed
by this worker. The runner inherits its supervisor's environment and sets
`TMPDIR`; injecting a session does not sanitize unrelated credentials or service
configuration. Its child launch uses `close_fds=True` and passes the science
socket and lifetime lease; standard output/error and external file access still
need appropriate deployment grants. The worker keeps the lease through process
exit. Direct-worker termination does not establish descendant containment.

Construct and control the host on its owning event loop. Ownership of the fresh
session transfers only after the host constructor returns successfully. A
configuration or constructor refusal before that leaves the session with its
caller. Put the host in an async context before starting science; its exit owns
original retirement even when startup raises or its waiter is cancelled.

`host.start(...)` requires an installed contribution class for metadata, an
installed `module:factory` constructor path, a startup mapping and resource
requirements as a `frozenset[ResourceRequirement]`. The child calls
`factory(startup=startup)`. There is no constructor
discovery or live-object transfer. Startup values use the bounded data encoding:
string-keyed mappings, lists/tuples, finite scalar values, bytes and explicitly
admitted SDK records. Convert provider settings to plain data and paths to
strings; arbitrary dataclasses, models and callables are not serialized.

Inside async startup, the existing NMRPeak assembly starts its HF contribution
as follows.
`host_options` contains the required `input_bytes`, `result_bytes`, `state_values`,
`progress_records`, `initialization_seconds` and `retirement_seconds`; the remaining
values come from its deployment configuration and supplied session.

```python
from nmrpeak_mock_provider.binding import Hf
from provider_core.process_host import ProcessExecutionHost

async with ProcessExecutionHost(session=process_session, **host_options) as host:
    hosted = await host.start(
        contribution_type=Hf,
        constructor="nmrpeak_mock_provider.process_factory:construct",
        startup={
            "contribution_class": Hf.__name__,
            "settings": offering_config["settings"],
            "checkpoint_path": str((PROVIDER_CONFIG_PATH.parent / offering_config["checkpoint_path"]).resolve()),
        },
        requirements=resource_requirements,
    )

    preparation_call = hosted.call(method=Hf.prepare)
    generation_call = hosted.call(method=Hf.generate)
    # Run and settle request lifetimes here, using these portable bindings.
```

Keep the contribution constructor lightweight. Acquire model resources in
`initialize(assets=...)`, after the SDK owns the contribution, and retain partial
acquisitions for `close()`. Resources hidden inside a failing constructor cannot
be recovered by the SDK. `start()` returns after initialization and declaration
agreement; cancelling its waiter leaves the original startup owned by the host.
Do not call `start()` again to replace that work. The returned hosted handle has
class metadata and allocation references, with no live parent receiver.

### Requests, limits and retirement

Provider assembly selects the preparation/generation bindings and prompt.
Request code uses `host.open_analysis(allocations=frozenset({hosted.allocation}))`,
its explicit reservation, `AnalysisExecution`, `PreparationTool` and
`Submission.from_call(...)` on either backend. Keep live-model lookups and
`submit_method()` out of that shared workflow. Prepared scientific values stay
in their worker; only encoded inputs/results and scoped state references cross
the channel. Consumption remains single-use, and a replacement allocation
cannot inherit old state handles.

All control stays on the original event loop. `open_analysis()`, `submit()` and
operation/analysis `request_stop()` are synchronous channel exchanges and can
block that loop for their two-second transport allowance. Async startup,
observations, result reads and cleanup retain their own I/O, but this backend
does not provide fully asynchronous, continuously responsive control.

The process channel permits at most **1 MiB per encoded input or result** and
**8 MiB per encoded frame**. Per-operation input/result allowances above 1 MiB
are refused before transmission, even if the host's aggregate budgets are larger.
There is no chunked transfer. Size checks on an input precede science; result
encoding can fail after scientific work has happened.

Retain a `Submission` before delivery and observe its original operation.
A validated `NotAdmitted` reply or the SDK's confirmed pre-admission
`StartDeadlineExpired` is distinct from an unknown reply. A lost, malformed or
timed-out reply retains the original operation and makes the channel unavailable;
it never grants permission to repeat science. Known completion/result facts
survive a later channel failure. Infrastructure failure does not become
correctable scientific input rejection, and arbitrary Python exception identity
does not survive transport. There is no automatic replay, restart or replacement.

Remote failures retain bounded causes and child file/function/line locations.
Oversized diagnostic text is omitted whole before transport so it cannot leave
a configured secret fragment. Protected operator diagnostics redact configured
secrets before their display limits; ordinary Python tracebacks remain raw
protected evidence.

Finish receiver/request tasks within the host lifetime. Result release and
analysis close start retained cleanup; observe them with `observe_cleanup` or
settle them with `settle_cleanup`. An expired wait or cancelled observer does
not restart cleanup or cancel its original work. Analysis capacity becomes
available again only after successful scientific close and its final handoff.
Retained results have their own release lifetime.

Host retirement gives outstanding startup, handoffs and scientific cleanup one
grace interval, then uses the runner session's separate native retirement
allowance. Deployment must budget for TERM, KILL, reaping and scratch cleanup.
Confirmed worker termination and scratch release settle native resources;
they do not establish that `Contribution.close()` succeeded or that science
produced a result. Scientific cleanup failures remain reported even after native
resources retire. Unconfirmed worker or scratch ownership remains unresolved
and unavailable. Preserve the original host and cleanup evidence until settled.

### Selecting the NMRPeak mock backend

The [NMRPeak startup](../../examples/nmrpeak-mock-provider/src/nmrpeak_mock_provider/provider.py)
defaults to `backend = "thread"` when the setting is absent. Process selection is
explicit: set `backend = "process"` under `[execution]`, retain exactly one
configured `[[science.hf]]` or `[[science.chf]]` offering, and supply a fresh
deployment-owned session. The shipped configuration contains two offerings and
must be narrowed. Both modes read host options from the existing
`[execution.in_process_execution_host]` table.

Process mode requires the installed mock `Model` class exactly; an arbitrary
injected model constructor is not a supported child factory. Its installed
[factory](../../examples/nmrpeak-mock-provider/src/nmrpeak_mock_provider/process_factory.py)
reconstructs settings in the child, and startup resolves the checkpoint path
before the worker enters its private directory. With deployment-supplied endpoint
factories, secrets, receiver and source text, the existing async request path is:

```python
from nmrpeak_mock_provider.fake_model import Model as MockModel
from nmrpeak_mock_provider.provider import open_offerings

async with open_offerings(
    create_model=MockModel,
    endpoint_factories=endpoint_factories,
    secrets=secrets,
    process_session=process_session,
) as offerings:
    (offering,) = offerings

    async with offering.open_prediction(receive=receive_report) as prediction:
        await prediction.interpret(source=source_text)
        await prediction.deliver()
```

The provider owns receiver, analysis and interpreter settlement in that order.
Further predictions can use the same offering sequentially. Thread mode accepts
the existing injected constructors and rejects an unused `process_session`.

## Retained scientific submissions

`provider_core.submission.Submission` retains one invocation before delivery can
have effects. Construct it from an existing `Invocation`, or use
`Submission.from_call(...)` to encode a compiled call's typed scientific input.
Construction does not submit work.

`submission.submit()` explicitly delivers that same request and returns either
its operation or the original `NotAdmitted`. After a lost reply, another submit
uses the same invocation. Once an operation has been acquired, submit returns it
without contacting execution again. The provider decides whether a refusal
permits another delivery attempt.

`await submission.wait()` observes the acquired operation without submitting,
stopping or releasing it. Cancellation leaves the original work running. Later
waits report current result state, including release; they do not cache success.
`ResultConsumption` separately owns a receiver attempt and result release.

Retained submissions use portable request identity, held until analysis closure.
`InProcessAnalysisExecutor.submit_method()` continues to request separate work on each
call. A request that must survive an ambiguous delivery should retain a
`Submission` before its first submit.

## Independent failures

`provider_core.failure_collection.FailureCollector` shares error collection while
providers keep their explicit cleanup phases. Wrap each attempted phase in
`with failures.capture():`, then call `failures.raise_if_any()` after all phases.
A lone original exception is raised directly; independent causes are grouped.
Later observations of the same cause are omitted by identity, including overlaps
inside exception groups. Existing groups remain intact when no filtering is needed.

The collector owns no resources, callbacks or tasks. Capturing cancellation does
not settle interrupted work: use the existing receiver, cleanup and host owners
for that. Providers still choose ordering, including Reference's delivery of a
prior usable report before observing a later failed analysis's cleanup.

## Type requirements

`provider_core.type_requirements` provides three explicit checks for ordinary
Python type contracts:

```python
from provider_core.type_requirements import require_instance, require_subclass, require_type

require_type(value, int)                # type(value) is int, not bool
require_instance(value, (int, float))   # isinstance(value, (int, float))
require_subclass(candidate, Base)       # candidate is a class and issubclass(candidate, Base)
```

Each accepts a class or nonempty tuple of classes, returns `None` on success,
and raises `TypeError` on failure. The failure reports the caller's argument
expression when source is available; otherwise it uses `repr(value)` (or the
concrete type name if `repr()` fails).
Source text and fallback values are not redacted. Use these helpers for
ordinary type requirements, not for protocol-specific rejection classes,
field-labeled errors or other domain validation.

## Scientific input tools

Providers using interpretation can expose an existing scientific method without
writing a second input schema. Install `provider-core[api]`; scientific records
remain ordinary frozen/slotted dataclasses.

```python
from provider_core.preparation_tool import PreparationTool

preparation_tool = PreparationTool(
    call_binding=hosted_predictor.call(method=ScientificContribution.prepare),
    state_output_slot=ScientificContribution.validated,
    analysis_executor=analysis_executor,
    reservation_ref=analysis_executor.reservation(allocation=hosted_predictor.allocation),
    operation_policy=operation_policy,
    description="Submit source-faithful spectrum data for validation.",
)
```

Pass `tools=[preparation_tool.definition]` and `dispatch=preparation_tool.dispatch` to `Interpreter.run`.
The SDK decodes proposals, executes preparation, retains `preparation_tool.prepared_state_handle` for the
provider's next scientific method, and releases the temporary validation report.
Input rejection permits model correction; operator failures retain their original
causes. A confirmed start-window expiry permits the next model to try after the
original work settles. `preparation_tool.resume()` resumes the original request or release
after cancellation or a lost reply, without submitting new science.

The provider selects the preparation method and its output port; the SDK does not
choose the subsequent scientific sequence. For tools that only advertise and
decode input, `provider_core.input_tool.ScientificInputToolDefinition` remains available independently.
The tool derives its name
from the compiled method and uses the input record's fields as parameters;
providers supply no protocol name, wrapper field or duplicate record definition.

Schema generation uses Pydantic internally; encoding and runtime admission still
use the original codec. Defaulted fields remain optional, nullable required fields
remain required, and unknown fields are forbidden in nested records. Definitions
and references remain rooted in the exported schema. Tool generation supports the
exact built-in JsonDataclassCodec, not custom codecs with guessed JSON semantics.
It does not force an endpoint's strict mode or replace scientific validation.

See [the research and tradeoffs](../../notes/087_scientific_input_tool_schemas.md)
and the standalone NMRPeak mock example for the full request lifetime.

## Attempts and capacity

Producer bookkeeping is live-only. The earlier `CheckpointStore`,
`AttemptRecovery` and store-taking lifecycle constructors have been removed.
Use the `.live()` factories below. The shared delivery queue continues to own
durable pending results and retries.

For live bookkeeping, use `AttemptController.live` with one shared
`AttemptClaims.live` registry in the provider's supervising process. The SDK
reserves capacity before START and keeps the actual controller reachable while
its Job is held. Unrelated Jobs may continue when capacity and their own
resources allow. The default registry reserves eight entries, 128 KiB of encoded
control metadata and up to 2 MiB of terminal-request bytes per entry. These are
bookkeeping allowances, not limits on the whole process's memory or permission
to run eight calculations at once. Providers set their own explicit limits.

The controller owns its original lifecycle task. `run(pending)` starts it once;
`join()` observes that same task. Cancelling either wait ends only that observation.
Use `request_stop()` or the supplied stop event to withdraw future START and
scientific effects, then join before `settle()`. Stopping cannot erase a selected
report, and the controller never sets the caller-supplied event. Keep the provider
lifetime and other resource owners alive while the original task is still running.

Supply a separate `admission_stopping` event when ordinary shutdown should stop
START and replay while admitted science drains. A late active START receipt stays
owned without entering science. The `stopping` event and `request_stop()` still
withdraw scientific permission; selected reports retain their delivery duty.
Without a separate admission event, the supplied `stopping` event governs both.

The phase-scoped `report_condition` callback also exposes
`report_condition.require_execution()`. Call it after awaits, immediately before
writing a scientific request or submitting work. The synchronous check reads the
current controller, claim and phase permission, including local stop, failed or
pending condition reports and an expired in-flight observation window. It works
in owned child tasks on the same event loop. It makes no API request and reserves
no permission across another await; already submitted work still needs settlement.
Core also checks at session-task entry. Test doubles must implement this method
explicitly; production bindings must use the supplied phase owner.

A deferred START retains its exact request and any possible-commit uncertainty;
it does not authorize a new key or another calculation. `ActiveStartReplayHeld`
returns the verified server receipt for an already active Attempt, without
entering science. That receipt identifies the Attempt; it does not show whether
earlier calculation began. Choose active-replay permission explicitly for each
provider. `LiveCapacityFull` identifies expected backpressure; settle the
rejected controller before waiting for capacity, and preserve independent cleanup
failures. Other ownership refusals are not capacity signals.

`AttemptExecutor.delivery_accepted` reports that the relay acknowledged the exact
terminal handoff. A provider may clear delivery backpressure for other Jobs at
that point, even if this executor still owns cleanup. This does not prove API
publication or permit releasing the full claim, replacing the worker, or
repeating science; those decisions still require their existing ownership checks.

A verified cancelled Job or expired Attempt before execution can free its
slot without a terminal report. Core preserves the original `ExecutionStopped`
in the controller failure. Once preparation and session retirement have
settled with no result or report pending, `executor.settled_revocation` exposes
the original server observation. The controller releases its claim during
the failed run if already settled, or at subsequent `settle()`. An uncertain
START, a local stop and a stop after execution entry retain their existing
disposition requirements.

Provider loops can pass their original `ControllerFailed` to
`await controller.settle_revocation(failure)`. It returns the original verified
server observation only after cleanup and claim release, including retirement
that outlived `run()`. A different or independently failed lifecycle returns
`None` without changing ownership. Late independent cleanup failures still
propagate. Cancelling this wait leaves original tasks owned; it never restarts
preparation or science. Record the stop and check the worker's readiness before
accepting another Job. This operation does not terminate hung cleanup.

## Crashes and recovery

`work_settled` confirms that original work, I/O and required native cleanup have
stopped. It does not permit losing an unacknowledged report. Use every owned
controller's `shutdown_ready`, together with the provider's other resource owners,
before ending the supervisor lifetime. A passive Job exclusion with no pending
outcome or report does not block `shutdown_ready` once cleanup has settled.
A returned outcome or exact report awaiting durable handoff cannot be discarded
merely because its I/O stopped. Preserve the shared private lifetime
root with `ProviderLifetime`; a container-private directory cannot exclude a
second container.

Raise `AnalysisOutcomeUnresolved` when science produced neither a verified result
nor a reviewed public failure, chaining the original exception. Core keeps the
Attempt held without sending FAIL or repeating science. It requests retirement
of the original work; cleanup may remain pending or fail.
In a service that can continue with unrelated Jobs, call
`controller.require_held_failure(pending, failure)` with the selected START and
original `ControllerFailed`. It checks that this exact Job and Attempt remain
quiescent and owned, then returns that same failure for protected reporting.

The check admits unresolved analysis, exhausted Upload recovery and an observation
gap under their distinct rules. Independent failures and unhanded outcomes still
block it. It does not settle or release the claim; the provider must also check its
own stop state and resource readiness before selecting another Job. Bindings relay
original errors; an exception type alone does not authenticate its source.

Only an original worker exit correlated to the current Attempt and scientific
phase can become `ObservedWorkerCrash`. Keep the exact acknowledgement received
by `AnalysisSession.prepare` for later execution. Timeouts, EOF and our own stop
or kill are not crash evidence. Record local interruption before any deliberate
science/control close, including timeout or cancellation cleanup. On unexpected
EOF/reset, use one bounded `RunnerSession.observe_retirement()` call before local
close. Unlike `retire()`, it does not request stopping. Retain that original task
if the caller's wait expires; a failed underlying lifecycle read does not permit
another reader. If no original exit is established before interruption, the
cause remains unknown. Do not impose a short retirement deadline on a healthy
warm worker.
Diagnostics must not prevent an admitted crash report or replace an already
chosen result or failure report.

`AsyncRunnerSession` retains the original native setup and retirement reader.
Its short observation wait can expire while the same reader continues; request
stop, then join that reader within the native session's aggregate cleanup budget.
Use `not_after` when an existing cleanup owner already has an earlier deadline.
The failure accessors retain exact native causes. Science exchange and confirmed
reader quiescence remain the provider's responsibility.

Use `contain_task_exit(callable, ...)` at retained Task entries so an unexpected
`SystemExit` or `KeyboardInterrupt` reaches the supervising owner as a group
containing the original failure. Independent cleanup must still run. This creates
no extra Task and grants no scientific failure report or cleanup confirmation.
The CLI reports a failed background operation through its safe error renderer
after settlement; it must not print the raw group's private exception text.
Runner retirement messages must include an explicit supported reason; messages
without one are rejected and cannot establish an observed crash.

Live bookkeeping disappears if its supervisor dies. Restart does not restore
START, calculate, poll old Attempts or fabricate failures from missing state.
Reports already accepted by shared delivery continue independently. If the
supervisor dies before handover is confirmed, the report may already be queued
or may be lost; the SDK does not reconstruct it on restart. Orderly shutdown
must instead wait for shutdown readiness and the provider's other resource
owners. Consumer migration and runtime qualification remain separate from the
SDK's contained tests.

## Shared result delivery

`api_settings_with_material` captures the original signing credential and optional
CA when the provider admits its API settings. Construct `SharedDeliveryClient`
with that material and a `DeliveryEndpoint` for the common intake socket. Its
`submit(outcome)` hands over the exact complete/fail bytes and publication inputs;
the acknowledgement proves durable local enqueue, not API acceptance. Delivery
reconstructs signing and TLS from each retained handoff and owns publication and
retries even after the provider exits. Lost acknowledgements and transport
retirement remain explicit outcomes.

The shared socket accepts submissions only: it has no queue inventory, status or
capacity-read operation. Queue and API proxy sockets are separate group grants;
the provider needs no other provider's credential, CA or local result store. The
host owns one operator settings file, durable queue and independently managed API
proxy. Providers retain their own API/scientific configuration and lifetime.
Read the shared project name and access GID from the provider's own deployment
settings, then use `SharedGrants(project, gid)` to project external named
volumes, read-only mounts with `nocopy`, socket paths and the supplementary GID.
One-off container launchers must verify those host-owned volumes already exist
before granting them; a Docker volume mount alone can create a missing volume.

Publisher notices keep the original report's Job, Attempt, binding and digest.
`publication_wait_seconds` measures this publisher's elapsed work on that
report, including retries; the existing queue overview separately measures
time spent waiting in the queue. A `delivery_published` notice has
`recovered=true` only when that report encountered a retryable failure or
stalled-publication warning before its matching Jobs API receipt and local queue removal.
Another report's publication does not clear a held or paused obligation.

A pending publication emits one `delivery_stalled` warning after its captured
API exchange allowance, retirement allowance and maximum retry delay have
elapsed in total. The notice identifies the original report, its elapsed wait,
and whether the Jobs API receipt has already arrived. This observation also
covers an offloaded queue update that remains blocked after that receipt.
The timer neither cancels storage nor changes the publication retry rules;
retries cannot renew it. It stops when the original publication call ends.
Logging still depends on a responsive event loop and the existing diagnostic
sink; this is not a storage recovery or process-restart mechanism.

Protected `ExecutionFailed` diagnostics include `failure_age_seconds`, measured
from the original lifecycle failure and preserved across cleanup observations.
The retained exception can outlive successful settlement: its age is historical
evidence, while the original controller remains the authority for claim release
and scientific readiness.

## Runner lifecycle

The native supervisor requires one explicit lifetime: `--operation-seconds N`
caps one child session, while `--owner-session-lifetime` allows a loaded child
to serve repeated scientific requests. Child exit, control-owner stop or
disconnect, and supervisor shutdown still trigger bounded retirement and scratch
cleanup. Startup, TERM, KILL and cleanup budgets remain mandatory. The provider
owns scientific readiness, request deadlines and cleanup between requests; the
control greeting does not prove that a model is ready.
The supervisor admits socket, scratch and status directories as canonical
absolute caller-owned mode 0700 directories and holds them for its lifetime.

`RunnerSession.request_stop()` retains a stop requested before connection and
wakes endpoint retries. Once connected, the original greeting reader retains
its startup deadline; join that setup operation before retirement or close.
Use one absolute provider startup deadline across control setup and scientific
readiness. A failed greeting prevents another control read, and closing the
session does not prove child or scratch retirement.

After setup joins, `control_connected` records whether control connect returned
successfully, including after close. It proves neither greeting admission nor
cleanup. Only the SDK's `RunnerStartupStopped` with no successful connection
identifies its pre-connection stop path; arbitrary OS failures, including an
ordinary `InterruptedError`, retain their original identity and uncertainty.

`RunnerStartupUnavailable` is a `TimeoutError` emitted only when the SDK's own
pre-connection deadline expires, before any attempt or after known missing or
refused endpoint attempts. Socket and greeting timeouts retain their original
identity and uncertainty; providers own the resulting availability policy.

## Conversations and tools

`ToolConversation` can run with an injected carrier in the native-only install;
it does not import HTTP libraries. Actual `ChatEndpoint` use requires the `api`
extra. Lightweight `chat_errors` and `transport_errors` own the failure types;
the existing `chat.ChatFailed` and `transport.ExchangeFailed` imports expose
those same classes.

`ToolConversation` keeps one bounded transcript and permits endpoint fallback
only after a carrier failure. Providers own endpoint selection and scientific
tool dispatch. An explicit `text_correction=TextCorrection(user_content=...)`
grant sends each safe rejection as plain tool text, followed by the exact
reviewed user instruction. It also permits absent/null tool-call replies to
consume a repair turn without inventing a tool result. Canonical admitted
arguments survive caller rejection; invalid or unparsed arguments remain
erased. Omitting the grant retains JSON rejection feedback and the existing
tool-carrier requirements. Both modes reject reused call IDs and keep the same
total turn budget and endpoint retirement requirements.

`TextCorrection()` supplies plain tool feedback without an extra user message;
it still requires an identifiable tool call. Set `argument_bytes` on the
conversation when raw argument JSON needs a tighter bound than the response.
The bound applies before decoding, so whitespace and encoding count too.

An awaited scientific dispatch may return `ToolFinished` only after preparation
is accepted, or raise `ToolRejected` to continue the same transcript. Keep its
scientific work deadline separate from model/inspection deadlines when those
budgets differ. No second conversation or suspend/resume framework is needed.
`ToolHandoff(reason)` returns a `ConversationHandoff` normally, leaving model
selection with the caller. The handoff is a next-step decision, separate from
accepted preparation and endpoint failures. Observe retirement before starting
the successor.

`ChatEndpoint` sends one request without resending failed model calls. It decodes
compressed bodies under `response_bytes` and optional `error_response_bytes`
(which defaults to the same limit). Its optional `not_after` argument bounds
request work with an absolute event-loop deadline; the transport's exchange
allowance still bounds each request. With `not_after`, the original exchange gets one additional
configured retirement allowance to close its resources. Repeated retirement
calls cannot renew it, and blocked cleanup remains owned. A received non-success HTTP head
survives a failed body read in `ChatFailed.cause.response_head`; this is protected
evidence, not a complete response. Response headers describe the wire entity
even when the retained body has been decoded. Providers own retry, fresh-prompt
fallback and the disposition of scientific outcomes.

## API behavior

Retry permissions describe which next request is allowed; the consumer owns
pacing, stop checks and deadlines. `can_retry_read` permits fresh GETs after
settled rate limits and server failures, or a complete correlated request-body
timeout. `can_retry_job_discovery` additionally permits paced Jobs reads while
reviewed authentication or authorization refusals may recover. Neither permits
science or promises that access will recover. Keep request-ID evidence for
operator diagnosis.

`can_refresh_hello` permits a freshly constructed current complete announcement,
including after a rate limit or server failure. It never replays an old snapshot
or confirms an earlier send. A verified request-body timeout proves only that
this send made no domain change; earlier uncertainty and retained START keys and
bodies remain owned by their lifecycle. Fresh-signing permission alone never
authorizes another mutation or capability issuance.

`AnalysisOffering` admits its kind and description when constructed, so providers
can attach their model-specific context to a refusal. Aggregate Hello encoding
owns offering uniqueness and the total request budget. CA loading distinguishes
non-ASCII input from OpenSSL certificate-loading failures, preserving protected
causes; the selected file reader owns file and byte-limit admission.

`HttpTransport.exchange_decoded` bounds wire bytes and each decoding stage under
the original exchange owner. It supports identity, complete gzip members and a
bounded deflate profile; see [decoded_body](src/provider_core/decoded_body.py)
for framing and compatibility limits. Direct model HTTP requires explicit opt-in;
signed API requests retain HTTPS verification.

Supply `not_after` when the caller already owns an absolute event-loop deadline
for request work. A response observed after that cap is not returned as success.
On interruption, cleanup of the original exchange has one separate, fixed
`retirement_seconds` allowance, including after caller cancellation. Subsequent
retirement calls share that deadline; completed cleanup can still be observed
after it expires. Neither cleanup observation nor an expired request cap permits
new request work. Without a cap, the configured exchange allowance bounds work
and each retirement observation retains its existing bounded wait; the caller
owns the overall recovery or shutdown deadline.

`provider_core.network.network_availability` classifies original native I/O
evidence from SDK-owned API or model HTTP exchanges. A positive result grants no
retry by itself. The caller still owns operation policy, confirmed retirement,
cancellation, pacing and the original deadline; model adapters retain their own
response and endpoint-fallback rules.

## Package and qualification

Use the [GitHub releases](https://github.com/numpde/nmr-provider-tools/releases)
on Python 3.12 or later. Each release supplies one wheel, its source distribution,
two installation locks and qualification evidence. The full `install.lock` includes
API dependencies; `native-install.lock` installs only that same SDK wheel for
scientific workers. For ordinary dependency declarations, request
`provider-core[api]` when using API or delivery modules.

Native lifecycle helpers and `provider_core.upload_file` use only the standard
library. Scientific readers can verify immutable file snapshots there without
installing HTTP or signing dependencies. Install offline from verified release
assets and cached third-party wheels; consumers do not rebuild the SDK or import
a sibling checkout. Contained launchers supply the package's container marker.

`UploadTransfer` owns verified file transfer and its original I/O cleanup when a
provider needs its own source selection, cache or retry policy. `JobUploads` uses
that same owner with its existing request, byte and deadline limits. A closed
file receipt still requires verified readback before scientific parsing.

With `JobUploads`, supply `UploadRecovery` to enable retries within those same
limits. Take ownership of returned data before reporting `recovery_failure`,
which lasts until the next source operation.
`UploadRecoveryExhausted` identifies a retryable outage whose bounded recovery
ended after its original resources retired; its cause retains the current outage.

`deployment_route` continues to support isolated fixture and other private routes.
Joint provider deployment instead borrows the one host-owned API proxy grant.

The source distribution carries generic test helpers and pinned API fixtures.
Those fixtures establish recorded contract/content compatibility, not publisher
authentication or live API availability. Synthetic provider adaptations qualify
their declared seams; real NMRNet and Magnet integration is proved by their own
consumer suites.

## Shared interpretation

Released in `provider-core[api]==0.6.3`. Provider owners should integrate this
contract, check their actual adapter with the installed package, then deploy
each ready existing deployment through its maintained targets. Keep source,
installed-package and running-deployment evidence distinct; SDK qualification
does not prove provider behavior or require waiting for other providers.

`provider_core.interpreter.Interpreter` owns one interpretation across an ordered
model ladder. It divides remaining time among untried models, fixes each model's
deadline, and retires its I/O before retry or escalation. SDK policy currently
allows 12 logical turns, 3 corrections per model, and at most 2 HTTP attempts per
turn. Availability failures retry the same endpoint with jitter and valid
`Retry-After`; permanent service failures advance immediately. A connected reply
can use its model window. The 10-second setup cap covers DNS, TCP or Unix, CONNECT
and TLS together; cleanup remains independently observed.

At provider startup, wrap each captured endpoint factory in a `ProseService`
and reuse that binding across Jobs. It preserves the existing retry delay,
including the HTTPS service's `Retry-After`, when the current Job has no time
or attempts left. Every subsequent HTTP send checks that shared delay against
its own original deadline. Bindings have independent state; a late successful
request cannot cancel a newer refusal received by another request. No global
cache or second HTTP retry loop is involved.

For admission, `service.retry_seconds` lets the provider postpone a Job it
already knows requires that unavailable prose service. If several services
can interpret the Job, the earliest available one determines that delay.
Typed input and independent services remain eligible. This scheduling hint
does not grant START, replace the Attempt permission check, or move a provider's
verified-input boundary. Plain endpoint factories retain the one-interpretation
behavior and do not preserve postponements between Jobs.

`ChatEndpointFactory` runs each outbound model HTTP exchange in a fresh Python
child. The parent retains the conversation, retry policy, tool dispatch,
Attempt and scientific worker. Only the admitted request and its transport
settings cross the bounded data pipe. Child startup, HTTP work and receipt of a
complete reply all consume the original absolute model deadline.

On interruption, the parent gives the original child one fixed
`retirement_seconds` allowance: cooperative cancellation, then TERM, then KILL
if needed, with exit observation in that same allowance. Repeated retirement
waits cannot renew it. A reply is admitted only after complete bounded framing,
EOF and successful child exit before the request deadline. An unconfirmed exit
retains the original owner and capacity obligation; later observation joins that
same cleanup. The original failure retains child evidence and observed process
facts for diagnostics. Recovery starts no new preparation or scientific worker.

Composition supplies synchronous endpoint factories, original messages, the
provider's existing tool schemas, and its async dispatch. Both
`dispatch(name, arguments, *, not_after)` and optional
`describe_constraints(tool, field, *, not_after)` receive the current fixed
absolute monotonic model deadline. Inspection uses it without resetting time;
admitted preparation retains its separate provider work deadline. Do not wrap
all dispatch in the model timeout or treat `ToolFinished(None)` as rejection.

A provider may raise `ModelWindowExpired` only after a timed-out inspection has
settled and its original reader can no longer produce an unobserved failure.
Independent work or cleanup failures still belong to the provider. The SDK then
retires the model before advancing. Ordinary provider timeouts, scientific
failures and uncertain cleanup still propagate without fallback. Neither the
deadline nor this API retires provider work on the caller's behalf.

Since 0.6.4, retirement racing an already-admitted preparation also preserves
its late `ToolFinished` result. Retirement prevents new dispatch and model turns;
it does not undo scientific acceptance. Keep the original run task and join it
through the provider's existing settlement owner. Do not cancel that task as a
substitute for stopping new model I/O or treat a retained result as permission to
execute after the Job has stopped.

`InterpretationInputRejected` identifies an initial-context credential refusal
before any model or scientific work starts. `InterpretationFailed` identifies
exhausted model allocations. Scientific callback failures retain their original
identity; do not classify them by matching generic `ConversationFailed` text.

Endpoint loaders can use `api_config.https_trust(captured_ca_bytes, label=...)`
to construct TLS trust without reopening files or supplying dummy transport
timeouts. `None` captures the runtime's system roots and default verification
policy. The shared function rejects ambient TLS overrides; providers still own
capturing their configuration and credentials.

Model factories require a context constructed by this helper or
`https_settings`. Capture preserves explicit PEM/DER anchors, fixed CA files
including trust/reject attributes and CRLs, and lazy hashed trust directories
with their exact lookup names. Supported TLS options, verification flags,
version bounds, ciphers, ALPN and ECDH settings follow that context into the
child. Trust capture is bounded to 4 MiB and 4096 material records. The parent
owns any private directory copies through confirmed child and pipe retirement.
This process carrier requires the Linux/POSIX facilities used by the provider
containers. Arbitrary prebuilt contexts and opaque features such as client
private keys, callbacks, custom socket classes, key logging, DH parameters and
NPN customization are refused explicitly when model transport is constructed.

Composition example:

```python
from provider_core.chat import ChatEndpointFactory
from provider_core.interpreter import Interpreter, ProseService

# spec, trust, proxy_socket and byte limits come from the provider's own config.
# Capture this service once, outside the per-Job interpretation function.
prose_service = ProseService(ChatEndpointFactory(
    spec=spec, trust=trust, proxy_socket=proxy_socket,
    retirement_seconds=retirement_seconds,
    request_bytes=request_bytes, response_bytes=response_bytes,
))

interpretation = Interpreter(
    endpoint_factories=(prose_service,),
    secrets=(spec.api_key,),
    **provider_config["interpreter"],
)
result = await interpretation.run(original_messages, provider_tools, dispatch,
                                  not_after=job_deadline)
prepared_input = result.value
# result.model_visit is the one-based index in the captured endpoint order.
```

For several factories, use the smallest `factory.context_bytes`, or a smaller
provider context limit. The factory derives available space from the same request
encoder as the carrier, including model and reasoning fields. Each call constructs
a fresh transport; the shared scheduler supplies its exchange and setup deadlines.

Dispatch returns `ToolReply` for observations, `ToolFinished` for accepted
preparation, and raises `ToolRejected` for a correctable proposal. Accepted
preparation ends interpretation. Scientific exceptions and caller cancellation
propagate without replay or model fallback. Exhaustion raises
`InterpretationFailed`; this ends the Job rather than prescribing a provider
shutdown. Retain the interpretation if retirement fails; its `retire()` can
reobserve every visited owner. Results retain bounded route observations and
private failures without placing them in the model's context.
If model cleanup fails after preparation was accepted, `accepted_result` retains
that exact preparation on the interpreter. Read it after settling the retained
resources; its presence proves neither model-I/O retirement nor execution
permission, and it does not authorize repeating preparation.
`failures` exposes model failures observed so far, including inside provider
callbacks before scientific effects and after retirement. It projects the same
owned facts as a result's failures without a second diagnostic store.
`correction_failures` on the interpreter, successful result and exhaustion error
retains `(model_visit, logical_turn, original_error)` tuples, including protocol
repairs before provider dispatch. The interpreter property remains readable after
retirement; these protected causes are never model context.
The caller still owns cancellation and joining of the `run()` task and settlement
of provider scientific resources. `retire()` and `retirement_confirmed` cover model
I/O; they do not prove an in-flight provider tool has stopped.
Provider dispatch exceptions propagate immediately, leaving `retire()` with the
caller. This permits prompt reporting of an established native crash while an
owned cleanup task is separately joined. Normal success and model transitions
settle retirement inside `run()`; a foreign scientific exception never escalates.
For the provider's `report_interpretation_problem` tool, dispatch raises
`ModelReportedInterpretationProblem(model_summary=safe_summary)` from its original
protected report cause. This immediately advances after retirement and records
only the explicit summary as unverified history. Raw report arguments are omitted
from history even when malformed report fields receive correction. It is not `ToolFinished`,
and agreement among models does not establish scientific input invalidity.
Internally this is a normal `ToolHandoff` decision; the original claim remains in
protected attempt diagnostics without inventing a failed model request.

A turn is one logical model request and its admitted tool action. Transport
retries remain inside that turn. Observation tools continue the conversation;
accepted preparation finishes it; an unverified input-problem report hands
control back to the model chooser. Tool names come from the supplied definitions
and are checked before entering provider code; scientific argument validation
remains with that provider.

Two ordinary tool objects bind each shared definition to its invocation;
advertisement and routing derive from those same objects. The module adds two information tools: `describe_input_constraints` reads the
actual supplied schemas (or an async provider-owned `describe_constraints`
accessor). That accessor returns `NotImplemented` for queries that should use
the SDK schema lookup; `None` remains an actual metadata value.
`read_interpretation_records` reads captured proposal/rejection
pairs and tool observations. Each model receives a compact catalogue and current
budget with every request. All observation and information replies carry `value` and
`interpreter_budget`; provider prompt/tool descriptions should account for this
envelope.

Stable provider instructions and original inputs lead each request. Changing
budget/catalogue state is appended as user-level context, keeping those facts
outside the instruction layer and preserving the stable prompt prefix.

History is immutable, in memory, and scoped to this interpretation;
retention is limited to 48 records and 1 MiB, with explicit unavailable counts.
Catalogue entries identify the tool, model visit, kind, size and historical status;
source/member identities remain in the provider-supplied captured arguments and
observations and are never inferred. Large reads return explicitly partial JSON
text and a per-record cursor. The
whole outgoing messages/tools context is checked against the granted byte limit;
the carrier also bounds the exact HTTP envelope. Neither proves a model token
limit. Provider tools retain authority over current source membership, inspection,
dynamic constraints and scientific truth. History never refreshes claims or
performs inspection, downloads, preparation or generation.

This API implements the tools-first workflow in note 050. Providers own migrating
their parsers, deployment configuration and adapters to the common contract in
notes 051 and 052. Reuse `ChatEndpointFactory` and `https_trust` alongside
`Interpreter`; remove superseded model schedulers and transport composition.
Provider-specific scientific admission, source inspection, preparation and
settlement stay with their existing owners. Exhaustion must not restart the
ladder through an outer availability retry or treat a model report as proof of
invalid input. Verify actual package/image identity, health and behavior after
deploying; imports and package tests alone do not complete provider adoption.

The implementation stays in `interpreter.py`: the interpreter schedules model
visits, each visit owns its conversation and cleanup, and immutable history
records own their metadata and pages. `ModelEndpoint` and `EndpointFactory` are
[structural typing contracts](https://typing.python.org/en/latest/spec/protocol.html);
adapters need no SDK base class. The structure follows
[PEP 8's guidance on narrow exception handling](https://peps.python.org/pep-0008/#programming-recommendations),
uses [frozen dataclasses](https://docs.python.org/3.12/library/dataclasses.html#frozen-instances)
for captured records, and preserves
[asyncio cancellation propagation](https://docs.python.org/3.12/library/asyncio-task.html#task-cancellation).
History captures arguments before provider normalization. Pagination measures the
prospective full context through its conversation owner, including the current
assistant call, reasoning and escaped feedback; one fixed size projection keeps
the prefix search stable while clock values change. Provider constraint-reader
failures propagate without model fallback; unknown property paths remain
correctable even under valid Boolean schemas.

Agent-loop references inspected on 2026-11-04:

- [Anthropic's async `_steps()`](https://github.com/anthropics/anthropic-sdk-python/blob/5752c132041af71055584632caab5ca2325cae5e/src/anthropic/lib/tools/_beta_runner.py#L1249)
  is the clearest reference for request, decision and tool-result ordering.
- [OpenAI's explicit next-step types](https://github.com/openai/openai-agents-python/blob/23e9996ac146109bdcd9b871e3564f70dc13aae1/src/agents/run_internal/run_steps.py#L163)
  inform the separate continue, handoff and final outcomes.
- [Anthropic's context-engineering guidance](https://www.anthropic.com/research/effective-context-engineering-for-ai-agents)
  supports the existing catalogue and on-demand retrieval design.
- [OpenAI's prompt-caching guidance](https://developers.openai.com/api/docs/guides/prompt-caching)
  supports placing stable instructions before changing context. Cache-hit and
  live-model performance improvements require separate deployment evidence.

## Source history

Repository names below identify [NMRNet](https://github.com/numpde/nmrnet-repro),
[Magnet](https://github.com/numpde/magnet-deploy),
[SECS](https://github.com/numpde/secs-repro),
[NMR API](https://github.com/numpde/nmr-api-v1), and
[NMRPeak](https://github.com/numpde/nmrpeak-repro).

Source and adaptation rationale is retained from NMRNet
`d09b77b951551c977e36bc87b5822315a4d1e944`:

- Signing follows NMR API `6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8`,
  `nmr_api/security/authentication/_rfc9421_profile.py` and
  `nmr_api/security/credential_refs.py`. Request construction adapts Magnet
  `c5813ee35cfbb1c98a5cc5d20a94787271f26486`,
  `magnet_provider/provider_http.py:611`: exact target/body bytes, Ed25519,
  caller-supplied time and nonce. Constructing a signature proves no acceptance.
- Transport follows that Magnet revision's `provider_http.py:688` exchange
  boundary. Core retains failed or unfinished cleanup and blocks reuse until
  retirement, rather than adopting best-effort release. Received metadata and
  file-output behavior also follow SECS
  `7356c354368f2355d3ab5cef00cf48c67cea9c2c`,
  `src/secs_inference/provider/http.py:290`. Historical note003 R7a/R7b means
  NMRNet `docs/notes/003_nmr_api_provider_20261018.html` at the donor revision.
- Chat adapts Magnet `600968a5fad8ad0118ea4f42cd7fc1fbfde54aaf`,
  `magnet_provider/openai_chat_interpreter.py`, and SECS
  `7356c354368f2355d3ab5cef00cf48c67cea9c2c`,
  `src/secs_inference/provider/chat.py:211`. Core owns bounded one-shot carriage;
  the provider owns tool dispatch and corrections. A lost reply does not grant
  another billed request.
- Upload transport follows the API revision above's `upload_transfer_http.py`
  and `upload_transfer_http_contract.py`; operation ordering follows SECS
  `34f5915c1534278bbf61b4c6592afa44cf94ab08`,
  `src/secs_inference/provider/job_api.py` and `upload_download.py`. Membership
  observation does not freeze later source changes or grant scientific admission.
- Producer checkpoint/recovery rationale comes from NMRNet
  `docs/notes/021_compose_runner_architecture_20261023.html`, superseding
  `017_provider_core_plan_20261018.html`. Producers retain compact control and
  handoff identity, never a parallel scientific-payload outbox or replay runner.

The pinned API fixture was copied through NMRPeak
`1697dc84dd1d76ae25b04efc8183fd80614000e5/contracts/upstream/nmr_api_v1`
and matches NMR API `daec9d978dacfd11b85ef96496dfd2e977e8142a`'s archived
release `719055db7751605c0e5325ca23bcbe66cde631376d4b2b99523c8e4db75f0d01`.
Its manifest SHA-256 is
`dda8dc36db963f85ec9105567342dae4ba680476c7ab1114bd1e33acba65474b`.
The manifest's declared source revision is excluded from that release digest;
content matching does not authenticate the publisher or prove live support.
No separate fixture redistribution-rights verification is claimed here.

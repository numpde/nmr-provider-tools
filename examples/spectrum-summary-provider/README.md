# Spectrum summary provider

This standalone provider illustrates the contribution and execution boundaries
in [the end-state design](../../notes/066_provider_execution_end_state.txt).
It runs with the SDK's real local `EmbeddedHost`. It does not implement the
planned common Jobs service, worker pool, durable delivery queue or remote backend.

## Responsibilities

```text
src/spectrum_provider/
├── science.py       portable spectra, settings and ordinary calculations
├── binding.py       configured contributions, codecs and stable revisions
├── settings.toml    baseline and minimum peak height
├── resources.toml   scientific CPU requirement
├── application.py   the provider's fixed baseline → peaks sequence
└── __main__.py      local deployment, permission and stdout consumption
```

`create_bindings()` is provider-owned assembly: it reads settings, constructs
`Baseline` and `Peaks`, and adopts them on separate allocations with explicit
`requirements`. Their exported methods read their own ordinary attributes. The
returned call inventories supply codecs and portable references to those exact
allocations. Assembly belongs inside the host cleanup scope so a failure while
adopting the second object still retires the first.

`PeakWorkflow` chooses the scientific sequence using only the public execution
contract. It does not import scientific types, codecs or the local backend.
The baseline output and peak input share the same portable `Spectrum` format.
The workflow reads the retained baseline result and passes those exact bytes to
the peak invocation. An accepted downstream operation owns those input bytes;
only then is the intermediate result released. No worker-bound `StateRef`
crosses between registrations.

The workflow retains the intermediate release handle separately. Its cleanup
failure must not hide an accepted peak operation or a usable final report; the
composition observes that release independently during cleanup.

The SDK owns generic observation waits, dispatch, execution-owned cancellation and start-deadline checks,
result consumption, retained results and observable cleanup. The bootstrap
chooses the scientific objects and SDK buffer and time limits. Resource
requirements describe what science needs; they do not grant hardware access.
One analysis reserves both allocations; this example makes no claim about fair
scheduling or sharing those allocations between concurrent analyses.

## Run and check

From the repository root, use the maintained installed-consumer lane:

```sh
make tests/integration/provider-core DOCKER_CONTEXT=science WHEELHOUSE=/home/ra/Downloads/wheelhouse
```

The lane builds the candidate SDK and each standalone example, installs their
wheels, and runs copied tests outside their source directories. No checkout
import or editable installation substitutes for the installed packages.

With the example and its matching candidate SDK installed in a permitted
container, run:

```sh
python -I -B -m spectrum_provider
```

Input intensities `(1, 4, 2, 5, 1)` at positions `(0, 1, 2, 3, 4)` become
`(0, 3, 1, 4, 0)` after subtracting the configured baseline of `1`. Interior
strict maxima at or above `2` produce:

```json
{"peaks": [{"position": 1.0, "height": 3.0}, {"position": 3.0, "height": 4.0}]}
```

## Ownership when waiting or delivery fails

`start()` stores the original operation before any await. Cancelling or timing
out `finish()` ends that wait; calling it again observes the original work.
Scientific success and encoded-result availability are checked separately, so
an encoding failure never triggers scientific replay. A downstream admission
refusal leaves the intermediate result retained.

The controller calls workflow methods serially. The bootstrap gives the final
operation, exact result reference and consumer to SDK `ResultConsumption`.
It retains one read/consumer task before awaiting it; cancelling a waiter and
resuming consumption observes that same task. The result is released only after
the consumer returns successfully. Known acceptance survives a release-request
failure, so re-observation never invokes the acknowledged consumer again.
Consumer failure remains the original failure and leaves the result retained;
the SDK never automatically resends it. Any acknowledgement uncertainty requires
receiver-specific reconciliation before the caller authorizes another attempt.
The demo's flushed stdout is local consumption, not durable queue acceptance or
central API publication.

Results remain readable after analysis cleanup until explicitly released.
Analysis close and host retirement are separately requested and observed;
independent cleanup failures retain their original causes. All operation and
result ownership here is in memory. Stable identifiers do not establish crash
recovery, forced interruption or remote deduplication.
The bootstrap calls the shared owner's shutdown join, `settle()`, before
analysis/host teardown. It finishes acceptance and the exact release observation
even if shutdown is cancelled again, then propagates the recorded interruptions
and failures. This join has no new timeout; the demo receiver is a local stdout
write. A caller needing a bounded wait must retain the owner and its resources.


Provider constructor settings are ordinary object attributes. The SDK does not
serialize or hash them. Call identity refers to the adopted allocation; scientific
provenance remains provider-owned. Local submission uses the actual receiver
(e.g. `method=contribution.generate`). Adoption takes `requirements=...`;
hardware availability must be established by the environment running the provider.

The SDK supplies execution identities. Use allocation handles with
`host.open_analysis(allocations=(hosted.allocation,))`, retain returned operation
handles, and observe those handles to resume waiting. Each new submission means
new work. State kinds and ports derive their names from Python types and class
attributes rather than manually assigned labels.

Adoption records resource requirements and establishes scientific-object ownership.
It does not discover or allocate CPU/GPU hardware. The returned allocation handle
identifies an execution owner, not a hardware grant.

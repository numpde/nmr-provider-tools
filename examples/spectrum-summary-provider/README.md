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

`science.py` can be called directly. It knows no Jobs, allocations, operation
handles, execution backend or report publisher. `create_bindings()` explicitly
loads packaged settings and creates two independent stateless registrations:
`spectrum.baseline/run` and `spectrum.peaks/run`. Configuration is immutable and
contributes to each registration's revision. Neither call needs an artificial
model instance or per-analysis scientific state.

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

The SDK owns generic observation waits, dispatch, guarded permission checks,
result consumption, retained results and observable cleanup. The bootstrap
separately chooses fixed
CPU allocations and capacity limits. Resource requirements do not grant access.
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

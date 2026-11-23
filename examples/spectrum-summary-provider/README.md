# Spectrum summary provider

Startup uses SDK `ExecutionSettings.open_contribution()` to own host creation,
model initialization and retirement. The provider keeps its scientific settings,
model/stage inventory and capacity shares. Both stages use the typed-call pattern in
[Reference Match](../reference-match-provider/). The scientific sequence is two
ordinary awaits; an SDK invocation scope retains that sequence when an observer
stops waiting.

This installed example composes two independent scientific stages: baseline
subtraction followed by peak detection. `science.py` owns scientific values and
calculations, `binding.py` declares the operations, and `provider.py` selects the
backend, owns the stage lifetimes and defines the two-call scientific sequence.
`process_factory.py` constructs each stage inside its installed worker.

`open_provider()` reads `config/deployments/localhost/provider.toml` from the
application's working directory. `execution.backend` explicitly selects `thread`
or `process`. Both modes open two independent SDK hosts and run the same
`summarize()` sequence. Class-bound call declarations supply codecs;
process execution constructs no scientific contribution in the parent.

For process execution, deployment supplies
`open_provider(process_sessions={"baseline": baseline_session, "peaks": peak_session})`.
Each fresh `RunnerSession` must select an existing science runner whose installed
command is `python -I -B -m provider_core.process_worker`. Deployment owns runner
endpoints, identity and native startup/retirement grants. Each host takes custody
of its own session when constructed and retires its one persistent child on exit.
Configuration and stage partition refusals precede that handoff. A stage that has
already entered its host lifetime is retired if later stage startup fails; sessions
not yet handed to a host remain with the caller.

## Capacity ownership

`execution.host` declares the enclosing input-byte,
result-byte and scientific-state allowances. `execution.stage_capacity.baseline`
and `.peaks` explicitly partition those allowances; startup rejects nonpositive
shares and any sum exceeding the enclosing allowance. The same static partition
applies to both backends. This example does not borrow idle capacity between stages
or implement a scheduler. Each stage's result share must cover
`operation_policy.reject_encoded_result_over_bytes` to admit a normal operation.
A smaller share deliberately refuses that stage's operation.

The SDK requires positive state capacity even for these stateless contributions,
so this deployment explicitly grants two state slots, one per host. This minimum
is SDK friction, not a scientific requirement. `progress_records` bounds retained
history per operation; it is not an aggregate shared capacity. Initialization and
retirement deadlines are per-host policy. Declared CPU requirements describe the
science and do not allocate hardware.

## Request and delivery ownership

`Provider.summarize(spectrum=...)` awaits baseline correction and passes the
returned typed spectrum to peak detection. Each `ScientificCall` owns admission,
result decoding and analysis cleanup. It returns only after cleanup; no live
scientific handles or result references cross stages. A baseline encoding or
cleanup failure stops the sequence before peak detection. Peak admission retries,
when enabled by `execution.error_policy`, retain the corrected input and do not
repeat baseline correction. Once those attempts are exhausted, the invocation
retains the failure; another wait does not replenish its budget. This replaces
the old caller-driven resubmission after each refusal. An uncertain submission
outcome never authorizes replay. Baseline cleanup must now succeed before peak
detection starts, replacing the old independent intermediate-release handling.

`open_invocation(partial(provider.summarize, spectrum=...))` starts this complete
sequence once. `invocation.wait()` observes its retained result or failure;
interrupting that wait leaves the original sequence running. A later wait rejoins
it, even if the first or second stage completed in the meantime. This scope must
remain inside `open_provider()` so both hosts outlive the sequence.

Leaving the invocation scope, or calling `invocation.close()`, cancels unfinished
work and joins the active call's cleanup. Repeated shutdown cancellation cannot
abandon that cleanup or retire the hosts early. Closing prevents the next stage
from starting; it does not undo a calculation already dispatched. The SDK retains
independent execution and cleanup failures. An overdue cleanup flag is not proof
that execution stopped.

The final report is an ordinary in-memory value, retained by the invocation and
its caller after scientific resources are released. Publishing it is a separate
application action: publication failure cannot restart either stage. The demo
writes it to flushed stdout once. This is neither durable delivery nor restart
recovery. Starting a new invocation explicitly starts new science.

## Run and check

From the repository root, use the maintained installed-consumer lane:

```sh
make tests/integration/provider-core DOCKER_CONTEXT=science WHEELHOUSE=/home/ra/Downloads/wheelhouse PRIVACY_TESTS=0 TEST_PATTERN=test_spectrum_process.py
```

The lane builds the candidate SDK and standalone example wheels, then runs copied
tests outside their source directories. The other Spectrum test filenames cover
scientific rules, binding declarations, delivery and cancellation.

With the wheel and matching SDK installed in a permitted container, run the demo
from a directory containing the deployment configuration:

```sh
python -I -B -m spectrum_provider
```

The configured baseline of `1` changes intensities `(1, 4, 2, 5, 1)` to
`(0, 3, 1, 4, 0)`. Interior strict maxima at or above `2` produce:

```json
{"peaks": [{"position": 1.0, "height": 3.0}, {"position": 3.0, "height": 4.0}]}
```

Real-model, GPU and deployment qualification remain separate gates.

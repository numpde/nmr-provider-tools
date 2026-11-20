# Spectrum summary provider

This installed example composes two independent scientific stages: baseline
subtraction followed by peak detection. `science.py` owns scientific values and
calculations, `binding.py` declares the operations, and `provider.py` selects the
backend and owns the stage lifetimes. `application.py` owns the scientific sequence.
`process_factory.py` constructs each stage inside its installed worker.

`open_provider()` reads `config/deployments/localhost/provider.toml` from the
application's working directory. `execution.backend` explicitly selects `thread`
or `process`. Both modes open two independent SDK hosts and run the same
`PeakWorkflow`. Class-bound call declarations supply codecs and references;
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

`Provider.create_workflow(spectrum=...)` encodes input before opening two stage
analyses. The caller owns both `baseline_analysis_executor` and
`peak_analysis_executor`: request stop and observe their cleanup before provider
exit. If opening peak execution fails, the original baseline host retains its
requested analysis cleanup. Both scientific stages keep independent lifetimes;
retiring one does not retire the other.

`start()` submits the retained baseline invocation. `finish()` observes that
original work, reads its retained result, and submits those exact portable bytes
to peak detection. It releases the intermediate only after downstream admission,
or after downstream execution closes. A capacity refusal retains the baseline
result for a later admission attempt. Scientific success and result encoding are
separate outcomes, so failed encoding never repeats science. Intermediate release
failure cannot hide an accepted peak operation or final report.

Cancelling a wait leaves the original submission and operation owned by the
workflow. The final result remains readable after analysis cleanup until release.
SDK `ResultConsumption` retains one receiver attempt across cancelled waits,
releases only after acceptance, and preserves the result if the receiver fails.
A caller must reconcile uncertain external acceptance before authorizing another
attempt. `__main__.py` demonstrates local flushed stdout consumption and joins the
receiver before closing both analyses and retiring the hosts. It also observes
intermediate-result cleanup independently. These owners are in memory; this
example makes no crash-recovery or durable-delivery claim.

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

# NMRPeak mock provider

This example models provider setup and one prose-input prediction workflow against
the SDK's target scientific interface. The scientific model and tokenizer are
small stand-ins. Tests replace the language-model endpoint and report receiver;
the interpreter, scientific execution, result ownership and cleanup are real SDK
components.

Provider startup does not run synthetic jobs. Tests invoke the same request
workflow that a surrounding provider service would invoke.

```text
config/deployments/localhost/
    provider.toml    scientific settings, checkpoint paths, execution and interpretation limits
models/
    hf.json          synthetic HF model data
    chf.json         synthetic CHF model data
src/nmrpeak_mock_provider/
    provider.py      provider setup and per-request lifetimes
    application.py   interpretation → validation → generation → report consumption
    binding.py       scientific methods, initialization and prepared-state ports
    science.py       scientific records, model interface and result admission
    checkpoint.py    verification and loading through one open file
    fake_model.py    replaceable tokenizer and model stand-ins
    interpreter.md   source-faithful extraction instructions
tests/
    fakes.py             scripted LLM endpoint and recording report receiver
    test_peak_example.py installed workflow and failure scenarios
```

## Provider setup

`open_provider` is provider-written assembly, not an SDK discovery hook. The
surrounding service supplies an explicit model constructor, ordered endpoint
services and known credential values for SDK diagnostic scrubbing. A service
shares `ProseService` objects between requests to retain endpoint retry delays;
each request gets its own `Interpreter`.

Setup reads `config/deployments/localhost/provider.toml` relative to the application's
working directory. The top-level `PROVIDER_CONFIG_PATH` in `provider.py` selects
that path; there is no configuration-path argument. Configuration stays outside
the wheel. Synthetic model data stays outside the wheel in `models/`; each JSON
describes its role and contains fixed candidates rather than learned weights.
Checkpoint paths in the TOML are relative to that deployment document.
Different deployments can select different scientific settings and
model constructors without changing the SDK.

`open_provider` iterates the configured scientific inventory and constructs each
scientific contribution with its own settings and model path, then gives that exact object to the
SDK. `[[science.hf]]` and `[[science.chf]]` select input families; omit a family or
repeat its array entry to enable any number of configured models. There are no
per-model IDs or fixed HF/CHF slots. The provider exposes an unordered frozenset
of hosted offerings. Adoption records resource requirements; it does not
allocate hardware. The deployment must provide the requested CPU/GPU resources.
Model loading begins when scientific work first needs that object. Interpretation
limits are passed to each new SDK interpreter, which validates them on construction.

The provider uses the SDK host context to retire scientific objects on exit,
including after partial assembly. SDK settlement joins original cleanup before
propagating observer interruptions; it does not force a running thread to stop. The surrounding service must finish its request contexts before leaving
it, for example by keeping its request task group inside the provider context.
Independent work and retirement failures retain their original causes.

## One request

After the surrounding service receives source text and selects one of
`provider.offerings`, its request handler uses this workflow:

```python
async with provider.open_prediction(
    offering=offering,
    receive=receive_report,
) as prediction:
    await prediction.interpret(source=source_text)
    await prediction.deliver()
```

Here `provider` comes from `open_provider`; `offering` is the selected hosted
scientific object. The service supplies `source_text` and `receive_report`, an
async callable accepting encoded report bytes. The test supplies these same
inputs using recorded source text, scripted model replies and an in-memory
receiver. It does not construct SDK execution sessions or arrange their cleanup.

`open_prediction` creates a request's interpreter and execution session. The SDK
ScientificInputToolDefinition derives the tool name and schema from the selected scientific method
and decodes its arguments through that method's existing input codec. Provider
code supplies the scientific description without copying fields or choosing a
schema through an HF/CHF switch.
The interpreter asks a language model to extract spectrum data, then asks the SDK
to run the offering's scientific validation method. Correctable input errors go
back to the language model; model-loading and scientific failures propagate as
their original exceptions. SDK admission means work was accepted for execution,
not that the input is scientifically valid.

Successful validation retains prepared input for generation. `deliver` requests
generation once, retains its operation and result, and creates one receiver attempt.
Concurrent or repeated delivery waits join that same attempt. Cancellation of a
wait does not authorize new generation or a resend. A receiver error remains the
original error; result bytes stay available while the request context is open so
the surrounding service can inspect or reconcile the outcome.

Leaving the request context ends that reconciliation window. Cleanup joins an
already started receiver before closing scientific execution, then retires the
interpreter. It still attempts independent cleanup after an error and preserves
independent causes together. A cleanup deadline only marks work overdue; it does
not force a running Python thread or receiver to stop.

The receiver defines acceptance. The test receiver records bytes in memory;
that proves neither durable storage nor API delivery. The synthetic checkpoints
and candidate rankings make no chemistry claim.

## Relationship to real NMRPeak

| Responsibility | This example | Current NMRPeak |
|---|---|---|
| Startup composition | `open_provider` | `provider_main.py` composes configuration, interpreter settings and runners |
| Per-request lifetime | `open_prediction` and `PredictionWorkflow` | `provider_process.py` creates an `NmrpeakAnalysisSession` per selected job |
| Interpretation | One SDK interpreter per prediction, shared endpoint services | `input_interpreter.py` creates per-Attempt interpretation owners |
| Scientific flow | Validate spectra, retain prepared input, generate candidates | Runner validation and generation |
| Results and cleanup | Retained SDK result and receiver attempt | Shared Attempt and delivery owners retain obligations |

This is an example of the target SDK boundary, not a claim that NMRPeak already
uses `InProcessExecutionHost`. The real provider uses separately supervised runners; here
science runs in SDK-managed threads. `checkpoint.py` follows NMRPeak's same-open-file
verification/loading approach. An open descriptor does not prevent in-place file
mutation; deployment must not overwrite checkpoint contents during use.

Job discovery, authorization, Hello/readiness, signal handling, durable delivery,
crash recovery and runner supervision belong to the surrounding service and are
not implemented here. NMRPeak also accepts structured input; this example covers
the prose interpretation route. There is no placeholder server or executable
that pretends to implement those omitted responsibilities.

## Run the installed tests

From the repository root:

```sh
make tests/integration/provider-core DOCKER_CONTEXT=science \
  WHEELHOUSE=/home/ra/Downloads/wheelhouse TEST_PATTERN=test_peak_example.py
```

The maintained lane builds and installs the SDK and example wheels, then runs
outside the checkout. Tests run both offerings through the public request
workflow, change beam size using external deployment configuration, and exercise
input correction, retained delivery, concurrent waits, cancellation and original
failure propagation. Provider tests cover checkpoint errors, partially loaded
models, consumed-input ports and candidate validation through the same startup
and request contexts.
Generic receiver and retirement guarantees are tested in the shared SDK; test
fixtures select external configuration and substitutes rather than assemble a
second SDK runtime.

The example declares `provider-core[api]` for shared schema generation. Scientific
records remain standard frozen/slotted dataclasses; providers need no Pydantic
models, decorators or imports. See
[the schema research and decision](../../notes/087_scientific_input_tool_schemas.md).

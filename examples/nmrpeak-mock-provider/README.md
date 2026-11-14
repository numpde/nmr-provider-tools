# NMRPeak mock provider

This example runs one HF and one CHF prediction. Provider startup reads deployment
configuration and creates the scientific objects and SDK execution manager.
`Prediction`, the provider application, asks the SDK to validate interpreted
input and generate candidates. Startup then consumes the resulting reports.

The model, tokenizer and language-model endpoint are small stand-ins. The SDK
interpreter, scientific execution and cleanup are real. The example uses no ML
libraries, credentials or network services; Job discovery and durable API delivery
remain responsibilities of the surrounding provider.

| Component | Example | NMRPeak counterpart |
|---|---|---|
| HF / CHF inputs | Separate immutable types | Formula/proton and formula/proton/carbon offerings |
| Interpretation | SDK `Interpreter` and `ProseService` | Existing shared interpretation flow |
| LLM service | `ScriptedEndpointFactory` | HTTP endpoint factory implementing the same SDK protocol |
| Scientific validation | Loaded tokenizer, required spectra and token limit | Runner validation before generation |
| Prepared input | SDK state, consumed once | Validated request retained until generation |
| Model loading | Retain the model before verifying and loading its checkpoint | Keep partially loaded components available for cleanup |
| Model/tokenizer | Small deterministic Python implementations | Loaded neural model and NMR tokenizer |
| Output admission | Candidate count and string bounds | Provider-owned candidate validation |
| Execution | SDK `EmbeddedHost` runs submitted scientific methods | Execution and cleanup inside the chosen deployment |
| Report consumption | SDK `ResultConsumption`, stdout or recording receiver | Retained result passed to the external delivery receiver |

```text
src/nmrpeak_mock_provider/
    science.py       scientific records, model interface and result admission
    binding.py       HF/CHF contributions, initialization and prepared-state ports
    checkpoint.py    verify and load through one open file
    application.py   interpretation → validation → generation
    composition.py   explicit provider assembly and interpretation tool schemas
    fake_model.py    stand-in tokenizer and neural model
    fake_services.py stand-in LLM endpoint and report receiver
    interpreter.md   source-faithful extraction instructions
    hf.json/chf.json tiny checkpoint substitutes
    __main__.py      run both offerings and settle their owners
config/
    deployments/localhost/
        provider.toml    scientific settings and execution policy
```

`run_demo` is provider startup. It reads the deployment file, constructs the HF/CHF
`Settings`, and passes them and the selected `Model` constructor to `adopt_offerings`.
That provider-written helper builds `Hf` and `Chf` using the packaged `hf.json` and
`chf.json` checkpoint substitutes. It asks `host.adopt` to retain each exact object
before the SDK calls its initialize method, where provider code loads the model.

A real provider supplies its checkpoint locations in its own assembly code.
The path locates a file; the expected digest determines which contents are
accepted. A replacement model implements the same scientific interface, so the
provider can keep the same validation, generation and cleanup sequence.

`checkpoint.py` follows NMRPeak's `families/nmrpeak/checkpoint_file.py`: verification
and deserialization use the same descriptor. Deployment must not overwrite an
installed file during use. This example does not invent a snapshot service or
claim that an open descriptor prevents in-place mutation.

`Prediction.interpret` asks the SDK `Interpreter` to turn text into structured
input. When the scripted language model proposes input, `Interpreter` invokes the
provider's `Prediction.dispatch`. That method decodes the proposal and submits
`prepare` through `EmbeddedAnalysis.submit_method`.

SDK admission means the submission passed its ownership, input-size and capacity
checks: the SDK creates an operation handle and queues the work. It does not mean
`prepare` has run or validated the input. When the executor is ready, the SDK
checks whether that execution session or operation has been stopped and whether
its original start deadline has passed. It then starts scientific execution.

If `prepare` rejects correctable input, `Prediction` returns `ToolRejected` to the
interpreter so the language model can propose a correction. Loading and execution
failures propagate as their original exceptions. Once `prepare` succeeds,
`Prediction` retains the prepared-input reference and releases its validation
report bytes. It then submits `generate` using that prepared input. Repeated waits
observe the saved operation; submitting a method again would request more work.

`run_demo` retains a `ResultConsumption` for each generated report. That SDK object
passes result bytes to the provider's receiver, then releases them after the
receiver returns successfully. A canceled wait can resume the same consumption.
Receiver failure leaves the result available without rerunning science or
resending automatically. This example's receiver flushes stdout; that establishes
local acceptance only. The synthetic checkpoints and rankings make no chemistry
claim.

Provider code requests cleanup by releasing results, closing execution sessions
and retiring the host. Cleanup timers only make an overdue flag observable; they
do not cancel cleanup. The example uses `observe_cleanup`, which keeps waiting
after that flag and raises any original cleanup failures. It does not log an
overdue warning by itself.

From this repository, build the SDK and example wheels and run the installed
tests outside the checkout:

```sh
make tests/integration/provider-core DOCKER_CONTEXT=science \
  WHEELHOUSE=/home/ra/Downloads/wheelhouse TEST_PATTERN=test_peak_example.py
```

With both installed inside a permitted container, run from this example project
root, which contains the maintained `config/` tree:

```sh
python -I -B -m nmrpeak_mock_provider
```

It prints an HF report and a CHF report. Process placement remains deployment-owned;
the example does not copy NMRPeak's private worker frames into the scientific API.


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

Each deployment's `provider.toml` supplies both scientific settings and execution
policy. The maintained file is `config/deployments/localhost/provider.toml`
relative to this example project root. Configuration is excluded from the wheel.
Supply its `config/` tree in the working directory;
the top-level `PROVIDER_CONFIG_PATH` in `__main__.py` selects the deployment file.
No command-line arguments are required.

Startup reads that file once, constructs HF/CHF `Settings` from its science
sections, and passes them and an explicit model constructor to `adopt_offerings`.
That provider-owned helper reads no configuration and chooses no model code.
The SDK receives the resulting scientific objects and their requirements.
A provider can choose another constructor in its own startup composition without
adding model discovery or imports to the SDK.

The installed-command test supplies configuration outside the wheel and changes
beam size between runs, verifying that deployment configuration changes the
scientific results without changing the installed application.

Adoption records resource requirements and establishes scientific-object ownership.
It does not discover or allocate CPU/GPU hardware. The returned allocation handle
identifies an execution owner, not a hardware grant.

Execution owns cancellation. Provider code calls `execution.request_stop()` to
refuse more work and signal unfinished operations while retaining results needed
by receivers. It calls `execution.close()` later to dispose those resources.
The interpreter supplies `start_not_after` for validation using the same event-loop
clock; the SDK checks it before admission and before science, including after model
loading. A deadline failure reaches the interpreter only after the original worker
has settled, or when admission was refused before any worker was started.

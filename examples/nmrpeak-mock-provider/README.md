# NMRPeak mock provider

This example shows how NMRPeak's components fit the configured-object SDK
boundary. It uses no ML libraries, credentials or network services. It starts
with a selected request and ends with report acceptance; it does not replace
the API's Job/START protocol, durable delivery or deployment supervision.

Every boundary is intended for the real NMRPeak migration. The model, tokenizer
and language-model carrier are stand-ins; the SDK ownership machinery is real.

| Component | Example | NMRPeak counterpart |
|---|---|---|
| HF / CHF inputs | Separate immutable types | Formula/proton and formula/proton/carbon offerings |
| Interpretation | SDK `Interpreter` and `ProseService` | Existing shared interpretation flow |
| LLM service | `ScriptedEndpointFactory` | HTTP endpoint factory implementing the same SDK protocol |
| Scientific validation | Loaded tokenizer, required spectra and token limit | Runner validation before generation |
| Prepared input | SDK state, consumed once | Validated request retained until generation |
| Model loading | Empty owner, verified checkpoint stream, `load` | Split the current loader so partial ownership survives failure |
| Model/tokenizer | Small deterministic Python implementations | Loaded neural model and NMR tokenizer |
| Output admission | Candidate count and string bounds | Provider-owned candidate validation |
| Execution | SDK `EmbeddedHost` and adopted contributions | Scientific execution owner inside the chosen deployment |
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

Assembly constructs `Hf` and `Chf` with scientific settings, a deployment-owned
checkpoint path and an empty-model constructor. These are ordinary constructor
arguments. `host.adopt` retains each exact receiver before initialization opens
the checkpoint or loads the model. The content digest enters scientific
configuration; a deployment path is only its locator. Replacing the stand-in
model requires an implementation of the same model interface, not a second
workflow or test-only execution mode.

`checkpoint.py` follows NMRPeak's `families/nmrpeak/checkpoint_file.py`: verification
and deserialization use the same descriptor. Deployment must not overwrite an
installed file during use. This example does not invent a snapshot service or
claim that an open descriptor prevents in-place mutation.

The SDK interpreter receives recorded assistant tool calls. Its real dispatcher
decodes each proposed scientific input and runs the contribution's validation.
Correctable input rejections return through `ToolRejected`; checkpoint, loading
and generation failures retain their original exceptions and are not sent to
the model as user corrections. Accepted input remains bound to its analysis and
allocation until generation consumes it.

The caller retains one `ResultConsumption` and resumes that same owner after
cancellation. Consumer failure leaves the result available; it does not regenerate
science or automatically retry delivery. The executable's flushed stdout means
local acceptance only, not durable API publication. All checkpoint contents,
tokenization and candidate rankings here are synthetic; no chemistry claim is made.

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

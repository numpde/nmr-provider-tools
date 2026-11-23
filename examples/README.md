# Provider examples

Each child directory is a standalone provider project with its own package and
tests. Examples consume the installed `provider_core` interface; they are not
modules inside the shared runtime package.

- [`dummy-provider`](dummy-provider/) demonstrates direct scientific tests,
  typed contribution bindings and explicit startup on either backend, retaining
  state, progress and cleanup through the same caller sequence.
- [`spectrum-summary-provider`](spectrum-summary-provider/) demonstrates two
  configured scientific contributions, a provider-owned scientific sequence,
  retained result transfer and a runnable local execution composition.
- [`reference-match-provider`](reference-match-provider/) demonstrates the managed
  typed-call API: reuse a reference index, deliver each report, and let the SDK
  handle retries, result release and analysis cleanup.
- [`nmrpeak-mock-provider`](nmrpeak-mock-provider/) demonstrates HF/CHF interpretation,
  checkpoint loading, tokenizer validation, consumed preparation and candidate
  delivery, using real SDK owners with lightweight model and service stand-ins.
  Startup can select independent persistent processes for its configured models;
  thread execution remains the default.

Common startup lives in SDK `ExecutionSettings.open_contribution()`: construct
the selected host, initialize science and retire it on exit. Process factories
run in the child; thread factories supply ordinary scientific objects. Dummy and
Reference own one host; Spectrum selects separate stage hosts; NMRPeak selects
one process host per model or a shared thread host. Providers retain scientific
configuration, inventory and capacity partitioning.

Reference binds `ScientificCall` through `ExecutionSettings.bind_call()`. Its
application only sequences queries; the SDK owns each request's analysis, result,
optional receiver and configured retries. An ordinary deployment can instead use
`ExecutionSettings.open_process_call()` to include runner lease validation and
session construction from configuration. See the
[managed-call guide](../packages/provider-core/README.md#managed-typed-calls).

Spectrum retains the baseline and peaks roles required by its fixed scientific
sequence. An unordered collection should not erase those roles.

Deployment choices live outside wheels in `config/deployments/localhost/provider.toml`.
Each example selects its document with `PROVIDER_CONFIG_PATH` in `provider.py` and
reads execution parameters and scientific settings at startup. Tests stage that
same configuration layout and invoke the production setup paths.

All four examples decode the common execution section with SDK
`ExecutionSettings`: explicit `backend`, `host`, `operation_policy` and
`resource_requirement`, plus optional `error_policy` for managed calls.
`open_contribution()` owns a selected host and initialized contribution. NMRPeak
thread mode instead assembles and initializes its shared host explicitly. Multi-host
providers keep their session inventories and capacity shares local, checking
aggregate shares with `require_capacity_partition` before taking any session.
Public host constructors remain independently usable and validate their options.
Multi-host assembly supplies its own expected keys to `require_sessions` before
constructing any host; the SDK checks session inventory without taking custody.

Use `science.py` for scientific values/calculations, `binding.py` for SDK operation
declarations and scientific resource cleanup, and `provider.py` for configuration,
backend and lifetime assembly. Installed `process_factory.py` modules reconstruct
contributions from explicit startup data. `application.py` is present when a
provider owns sequencing decisions; Dummy deliberately leaves its minimal two-call
sequence to the caller. Mock models, custom payload codecs and prompts exist only
where their scientific example needs them. Each example's README describes its
startup, workflow and maintained installed tests.

Shared execution mechanics belong in the SDK. After observing completion,
`observation.require_result()` returns a usable result handle or raises the
original execution failures. Providers retain scientific decisions such as
whether rejected interpretation can be corrected, which operation follows a
baseline, and whether an earlier report must still be delivered after later work
fails. Matching configuration values do not make deployment choices SDK policy.

`Submission` retains each scientific request before delivery and owns its acquired
operation. Repeated explicit delivery uses the same invocation; waiting never
submits work. Preparation, Spectrum's stages and NMRPeak generation use that SDK
owner. Providers still decide whether to correct input, retry admission or move
to the next scientific stage.

`ResultConsumption` is an async context manager. Entering does not start delivery;
`consume()` starts or joins its original receiver attempt. Context exit settles
that same attempt before outer resources close, preserving independent errors.
It does not resend a failed receiver or decide when scientific execution stops.

All four examples use SDK contexts for original retirement and error propagation.
Reference delivers each report through the managed call before starting the next
query. Stateful multi-stage workflows retain their explicit request lifetimes;
receiver/request tasks finish inside the host context. SDK
`settle_cleanup` defers cancellation while observing pending cleanup; the existing
`observe_cleanup` remains an interruptible wait.

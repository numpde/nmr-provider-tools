# Provider examples

Each child directory is a standalone provider project with its own package and
tests. Examples consume the installed `provider_core` interface; they are not
modules inside the shared runtime package.

- [`dummy-provider`](dummy-provider/) demonstrates direct scientific tests,
  typed contribution bindings and one installed execution check for retained
  state, progress and cleanup.
- [`spectrum-summary-provider`](spectrum-summary-provider/) demonstrates two
  configured scientific contributions, a provider-owned scientific sequence,
  retained result transfer and a runnable local execution composition.
- [`reference-match-provider`](reference-match-provider/) demonstrates a real
  reference index reused across sequential analyses, with independent result
  retention and allocation retirement.
- [`nmrpeak-mock-provider`](nmrpeak-mock-provider/) demonstrates HF/CHF interpretation,
  checkpoint loading, tokenizer validation, consumed preparation and candidate
  delivery, using real SDK owners with lightweight model and service stand-ins.
  Its startup can explicitly select one persistent scientific process; thread
  execution remains the default.

Thread setup constructs ordinary scientific objects and gives each one directly
to `host.adopt`. Process setup supplies class metadata, an installed factory and
startup data to `ProcessExecutionHost.start`; the model is constructed and retained
in the child. Both paths select portable call bindings for the same request
workflow. The SDK owns validation, custody, execution and retirement;
provider-specific TOML parsing and scientific class selection stay in the provider.
NMRPeak's thread configuration can offer several models of one input family;
its current process profile requires exactly one configured offering, the installed
mock `Model` and a deployment-supplied fresh `RunnerSession`. See the
[current process-host guide](../packages/provider-core/README.md#scientific-process-hosting)
for startup/request usage, deployment grants, payload and control-call limits,
cleanup semantics and recorded installed qualification.

Spectrum retains the baseline and peaks roles required by its fixed scientific
sequence. An unordered collection should not erase those roles.

Deployment choices live outside wheels in `config/deployments/localhost/provider.toml`.
Each example selects its document with `PROVIDER_CONFIG_PATH`. Spectrum, Reference
and NMRPeak read execution parameters and provider-specific science settings once
at startup. Dummy only reads its call resource requirements, leaving host and
operation policy to the caller. Tests stage that same configuration layout and
invoke the production setup paths.

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

NMRPeak, Reference and Spectrum use the SDK host context for original retirement and error
propagation. Reference deliberately keeps analysis cleanup explicit: an earlier
usable report must be delivered before waiting for a later failed query's
cleanup. Receiver/request tasks must finish inside the host context. SDK
`settle_cleanup` defers cancellation while observing pending cleanup; the existing
`observe_cleanup` remains an interruptible wait.

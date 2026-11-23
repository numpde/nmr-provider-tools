# NMRPeak provider example

Process startup uses SDK `ExecutionSettings.open_contribution()` to own host creation,
model initialization and retirement. Thread offerings share one explicitly owned
host. The provider keeps its scientific settings,
model/stage inventory and capacity shares. For a single typed result, use the
managed-call pattern in [Reference Match](../reference-match-provider/); this
example retains explicit workflow control where state or multiple stages require it.

`science.py` owns scientific values and calculations. `binding.py` declares the
HF/CHF operations and model/state cleanup. `provider.py` reads configuration,
selects the backend and interpretation prompt, and owns prediction lifetimes.
`application.py` retains the same preparation, generation and receiver workflow
on both backends. `fake_model.py` supplies the installed mock model, while
`process_factory.py` constructs that model's contribution inside its worker.

HF accepts proton spectra; CHF accepts proton and carbon spectra. Their startup
mapping pairs each scientific contribution with its interpretation prompt. It is
specific to this provider, rather than an SDK model registry.

The shipped configuration explicitly selects `execution.backend = "thread"`. For process execution set
`execution.backend = "process"` and supply `process_sessions` to `open_offerings`.
The mapping has exactly one fresh deployment-owned `RunnerSession` for each
`(family, zero-based configuration index)` pair, for example `("hf", 0)` and
`("chf", 0)` for the shipped configuration. These keys select startup entries;
they are not durable request or offering identities.

Each process owns one model and one allocation. Every configured entry declares
`process_capacity` shares for aggregate input bytes, result bytes and state values.
Their sums must fit the provider allowances in `execution.host`.
An unused share cannot be borrowed by another worker. Progress history is capped
per operation; initialization and retirement allowances apply per host. Hardware
memory, GPU and CPU grants still belong to deployment; these SDK counters do not
reserve hardware.

Thread startup accepts an injected model constructor for local science tests.
Process startup uses the installed mock `Model` exactly; porting a real model
requires that provider's installed construction and its own device qualification.

Capacity and session-inventory refusal precede host construction. Each constructed
host then owns its session, and entered hosts retire if a later model fails.
The caller retains sessions that were never taken. Finish prediction contexts
before exiting `open_offerings`. Retiring one process offering leaves the other
workers available; provider exit joins every original host lifetime.

The installed integration selection is `TEST_PATTERN=test_peak_example.py` on
`make tests/integration/provider-core`. It uses the real installed SDK and native
runner, with mock science and endpoint services. It does not qualify a real model
or GPU deployment.

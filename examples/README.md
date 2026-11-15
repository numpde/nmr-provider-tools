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

Shared execution mechanics belong in the SDK. After observing completion,
`observation.require_result()` returns a usable result handle or raises the
original execution failures. Providers retain scientific decisions such as
whether rejected interpretation can be corrected, which operation follows a
baseline, and whether an earlier report must still be delivered after later work
fails. Matching configuration values do not make deployment choices SDK policy.

`ResultConsumption` is an async context manager. Entering does not start delivery;
`consume()` starts or joins its original receiver attempt. Context exit settles
that same attempt before outer resources close, preserving independent errors.
It does not resend a failed receiver or decide when scientific execution stops.

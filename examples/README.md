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

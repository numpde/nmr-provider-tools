# Provider examples

Each child directory is a standalone provider project with its own package and
tests. Examples consume the installed `provider_core` interface; they are not
modules inside the shared runtime package.

- [`dummy-provider`](dummy-provider/) demonstrates direct science, typed
  contribution binding, provider-owned conformance scenarios, one and two
  allocations, retained state, progress, rejection/correction and result cleanup.

# Dummy scientific contribution

This directory is deliberately **not** a provider service or miniature copy of
the planned shared platform.

A provider contributes only:

- `science.py`: ordinary provider-owned scientific code. It imports no
  `provider_core` module.
- `binding.py`: the adapter that binds configuration and scientific functions to
  the declarations in `provider_core.contribution`.

It does not contain a CLI, Jobs client, interpreter, scheduler, execution host,
worker pool, allocator, permission owner, supervision loop, recovery mechanism,
result delivery, drain logic or deployment code.

The shared package owns those concerns. Its focused tests in
`tests/native/test_embedded_host.py` submit fake work to this contribution and
exercise one or several allocations, state affinity, permission, result custody
and cleanup. `test_dummy_provider_boundary.py` keeps the source boundary explicit.

A real provider should replace the fake science and adapter while reusing the
shared operational machinery. It should not copy the test host or service logic
into its own package.

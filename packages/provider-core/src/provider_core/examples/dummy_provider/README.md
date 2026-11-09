# Dummy scientific contribution

This directory is deliberately **not** a provider service or a miniature copy of
the shared platform.

A provider contributes only:

- `science.py`: provider-owned dataclasses and scientific functions. It imports no
  `provider_core` module.
- `binding.py`: configuration/assets and the adapter to
  `provider_core.contribution`.

Portable inputs and results are provider-owned dataclasses. Shared codecs perform
bounded structural decoding and encoding; the adapter receives typed values, not
JSON dictionaries, transport lists or anonymous tuples. Retained live state is
bound into a provider-owned state dataclass before scientific invocation.

The package contains no CLI, Jobs client, interpreter, scheduler, execution host,
worker pool, allocator, permission owner, supervision loop, recovery mechanism,
result delivery, drain logic or deployment code. Shared tests own those concerns.

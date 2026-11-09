# Dummy scientific contribution

This directory is deliberately not a provider service or a miniature copy of the
shared platform.

The provider contributes only:

- `science.py`: ordinary scientific code. It imports no `provider_core` module.
- `binding.py`: the adapter from that code to `provider_core.contribution`.

The binding supplies two things.

First, it supplies a data-only description that orchestration can inspect without
loading the science. It fixes the implementation revision, configuration digest,
asset digests, named calls, state inputs and outputs, resident and per-call resource
requirements, process-isolation requirement, concurrency requirement and
cooperative-interruption support.

Second, it supplies host-local callables for model construction, initialization,
scientific input decoding, invocation, result encoding and cleanup. Those callables
receive scientific objects. They do not receive Jobs controllers, credentials,
claim registries, worker pools, allocators or Docker access.

A call adapter may receive `ScientificControl`, which exposes only a stop flag and
bounded progress reporting. The dummy uses it to report calculation progress. The
plain numerical functions in `science.py` still know nothing about execution.

The shared package owns admission, permission, reservations, operation identity,
state references, result custody, worker supervision, retirement and delivery.
Shared tests submit fake work to this contribution. Nothing in this directory
implements those responsibilities.

A real contribution should replace the fake science and binding while retaining
this boundary. It should not copy `embedded_host.py`, service loops or test
orchestration into its own package.

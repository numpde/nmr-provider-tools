# provider-deploy

One shared operation for deployment callers: **measure a container's host UID/GID
maps, retire the exact probe, then return the maps**. Providers keep their image
selection, Docker endpoint selection, input files, permissions and rollout logic.

This replaces duplicated probe lifecycles. It is not a deployment framework,
permission manager or persistent recovery service. Python 3.11+, Linux and the
Docker CLI are required; the Python package has no runtime dependencies.

The candidate is version `0.2.0a1`. It replaces the unreleased `0.1.0` prototype's
context/journal interface; it is not a compatible drop-in for that interface.
Build and install the reviewed local wheel explicitly rather than reusing an
older wheel with the same package version.

## Python caller

```python
from provider_deploy import ProbeRequest, probe_identity

maps = probe_identity(ProbeRequest(
    endpoint="unix:///run/user/1000/docker.sock",  # already selected by the caller
    image="approved-provider:local",             # must already be present
    interpreter="/usr/local/bin/python3.12",     # inside that image
    user_uid=65532,
    user_gid=65532,
))
host_uid = maps.uid_map.translate(65532)
host_gid = maps.gid_map.translate(65532)
```

`uid_map` and `gid_map` are validated `IdMap` values. `translate()` rejects an
unmapped or invalid ID with `ValueError`; calculate all needed identities before
changing permissions. No temporary directories, recovery files, Docker context
lookups or cleanup callbacks are required from the caller.

An optional `executable=` selects the Docker executable (for example, the
provider's already-validated `/usr/bin/docker`). An optional `cancel=` accepts a
`threading.Event`. `userns_mode` is explicitly `"default"` or `"host"`.

## Shell caller

```sh
python3 -m provider_deploy identity \
  --endpoint "$NMRNET_DOCKER_ENDPOINT_INPUT" \
  --image "$controller_image" \
  --interpreter /opt/provider/environment/bin/python \
  --user-uid 0 --user-gid 0 \
  --uid 0 --uid "$caller_uid" --gid 0 --gid "$caller_gid" \
  --values
```

On success, `--values` prints one line of unsigned decimal numbers: requested UIDs
in order, then requested GIDs in order. Duplicate requests retain their positions.
All projections must succeed before any number is printed. On failure stdout is
empty and stderr contains one JSON diagnostic. This lets shell callers use
`read`, without `eval`, map-file parsing or a second cleanup implementation.

Without `--values`, the CLI writes one structured success or failure to stdout.
Exit codes are 0 success, 1 settled failure, 2 invalid arguments, 3 unresolved
resource outcome, and 130 settled cancellation. Uncertainty takes precedence over
the cancellation exit code. No failure payload contains usable maps.

## What the operation guarantees

- Uses the caller-selected local Unix endpoint throughout. Ambient Docker
  contexts, client configuration and proxy settings cannot select another engine.
- Requires the host's initial user/PID namespaces and genuine host `/proc`.
  Container-relative identity maps are not accepted as host identities.
- Pins the local image to its immutable ID. It never pulls images, and rejects
  images that declare implicit volumes.
- Runs the supplied Python interpreter with `-I -B` and a fixed finite sleep
  program carrying a random token. A single `ProbeSpec` defines both the Docker
  creation flags and the configuration that inspection must verify.
- Restricts the probe to no network, a read-only root, no added capabilities,
  devices or mounts, no privileged/host-PID mode, and no-new-privileges. Fixed
  limits are 64 MiB RAM with no additional swap, 8 PIDs and 0.25 CPU. The memory
  and combined memory-plus-swap limits are both 64 MiB, following Docker's
  [memory/swap semantics](https://docs.docker.com/engine/containers/resource_constraints/#--memory-swap-details).
  Health checks and restarts are disabled; automatic removal is enabled as a
  second line of defense.
- Binds maps to exact container ownership, live host PID, process start time and
  procfs identity, with repeated map/process checks. It detects substituted,
  restarted or exited probes rather than releasing stale evidence.
- Removes only the verified exact container ID. Success requires confirmed
  container absence and retirement of observed probe processes on the same
  selected engine. Cancellation still enters bounded cleanup.

The fixed resource profile matches the former NMRNet probe's 64 MiB/8-PID limits;
it is larger than NMRPeak's former 32 MiB/4-PID limits. This is an explicit shared
profile, not a claim of identical provider policy or real-image qualification.

Default bounds are 30 seconds for measurement, 5 seconds per Docker operation,
15 seconds for cleanup and 90 seconds for the probe process. `Bounds` allows
Python callers to change them; the CLI exposes only `--timeout` for measurement.
Cleanup has a separate budget so cancellation/timeout does not abandon a probe.
Filesystem stalls and uninterruptible kernel work cannot be given hard wall-clock
guarantees by this Python process.

## Failures and limits

`ProbeFailure` separates three facts:

- `problem`: the primary stage, stable category and safe explanation.
- `retirement`: `known-not-created`, `confirmed-retired` or `uncertain-outcome`,
  the known container ID, and any secondary problems.
- `handle`: the pinned endpoint, random operation token, known exact container ID
  and observed process identities. It is diagnostic evidence, not a retry command.

For example, a changed map can invalidate measurement while retirement still
succeeds. A lost create reply is different: an empty immediate inventory does
not prove that Docker will never finish that request. The operation never issues
a second create and never turns uncertainty into a successful mapping.

**There is no durable replay or automatic recovery API.** Callers decide how to
retain diagnostics. SIGKILL, interpreter failure or host loss can prevent cleanup
and lose the in-memory handle. The finite process lifetime plus Docker `--rm`
helps once the probe has started; an unstarted container can remain. Unresolved
outcomes require operator review on the same engine, not blind retry or pruning.

Successful maps are point-in-time evidence, not permanent permission authority.
The provider must keep its selected engine and namespace policy consistent with
the subsequent deployment steps.

## Verification

The maintained aggregate includes `make tests/unit/provider-deploy
DOCKER_CONTEXT=<local-context>`, using the existing prepared checker with no
Docker socket mount or additional container grants. That lane tests source and
failure contracts, not a nested Docker engine. For the standalone host lane:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/provider-deploy/src \
  python3 -m unittest discover -s packages/provider-deploy/tests -v
```

Tests cover real bounded subprocesses, genuine process-map observations and a
stateful fake Docker executable whose mutations can succeed while replies are
lost. The latter is a protocol/failure test, **not a real Docker qualification**.
Its host-precondition/socket seams are substituted where the sandbox cannot
provide them; separate tests exercise the real rejection checks.

Real rootful/rootless engines, actual provider images, non-root host callers and
restart behavior still require qualification before deploying the migrated
providers. Keep those unrun cases distinct from passing local tests.

# provider-deploy

Host-side deployment operations, separate from `provider-core` and scientific
workers. This first slice is one complete **selected-local-Docker identity
probe**. It does not grant ACLs, change ownership, stage inputs, deploy services,
pull images, or migrate provider entry points.

The implementation is present; **real rootful/rootless engine qualification and
provider adoption are still pending**. Do not treat recording-engine tests as
production qualification or use the result as permanent permission authority.

## Explicit inputs

Run on Linux in the host's initial user **and** PID namespaces, with genuine
host `/proc` visibility. Supply an explicit Docker context, approved image
already present on that engine, container user and user-namespace mode,
requested UIDs/GIDs, separate operation/command/cleanup bounds, finite probe
lifetime, and a new recovery-journal pathname. There is no ambient engine
fallback and no image acquisition. Remote/non-Unix contexts are unsupported.

The approved image must have `/bin/sh` and `sleep`, permit the requested
numeric user, and declare no implicit volumes. The fixed short-lived command
keeps the unpredictable operation token in the shell's argv. Creation uses
`--pull=never`, an immutable inspected image ID, no network, a read-only root,
no capabilities, no-new-privileges, a PID limit, disabled image health checks,
and no automatic restart or anonymous volumes.

Install from this source tree with your controlled Python packaging workflow.
Python 3.12+ and the Docker CLI are required. Runtime Python dependencies are
standard-library only. This is not a package-publication or release commitment.

## Python API

```python
from pathlib import Path
from provider_deploy.model import Bounds, ProbeFailure, ProbeRequest
from provider_deploy.operation import probe_identity

request = ProbeRequest(
    context="rootless",
    image="approved-probe:local",  # caller prepares this image separately
    user_uid=1000,
    user_gid=1000,
    userns_mode="default",        # explicit: "default" or "host"
    requested_uids=(0, 1000),
    requested_gids=(0, 1000, 2000),
    journal_path=Path("/absolute/private-0700-directory/unique-operation.jsonl"),
    bounds=Bounds(operation_seconds=30, command_seconds=5,
                  cleanup_seconds=15, lifetime_seconds=90),
)
try:
    result = probe_identity(request)
except ProbeFailure as failure:
    report = failure.as_dict()    # category, state, cleanup failures, recovery handle
    # No mapped identities are present; resolve this operation before retrying.
else:
    report = result.as_dict()     # mapping is released ONLY after exact retirement
```

An optional `threading.Event` passed as `cancel=` cooperatively cancels the
operation; cancellation enters its independent cleanup budget. The CLI converts
SIGINT, SIGTERM and SIGHUP to this cancellation request. Forced termination, interpreter
failure, host loss, filesystem stalls, or an unreachable engine cannot guarantee
cleanup. The durable journal and finite process lifetime cover recovery evidence,
not guaranteed container deletion. The operation does not silently retry any
possibly successful creation or removal.

All time values must be finite and at most 3600 seconds; lifetime is a positive
integer. Requested ID sets are immutable tuples of 1–256 unique IDs each, from
0 through 4294967294. The all-ones Linux ID sentinel is never mapped.

## CLI

The CLI calls the same operation, not a collection of setup/cleanup fragments:

```sh
provider-deploy identity \
  --context rootless --image approved-probe:local \
  --user-uid 1000 --user-gid 1000 --userns default \
  --map-uid 0 --map-uid 1000 --map-gid 0 --map-gid 1000 \
  --journal /absolute/private-0700-directory/unique-operation.jsonl \
  --operation-seconds 30 --command-seconds 5 --cleanup-seconds 15 \
  --lifetime-seconds 90
```

`python -m provider_deploy` is equivalent. Stdout is one bounded JSON object;
diagnostics go to stderr. Exit codes: 0 success, 1 failed operation with a
settled resource state, 2 argument error, 3 uncertain resource outcome, 130
cancellation. Failure JSON never contains a success mapping or arbitrary engine
stdout/stderr. Individual stable categories distinguish host preconditions,
invalid maps, ownership/process identity, engine changes, timeout, cancellation,
and retirement/reconciliation failures; `cleanup_failures` preserves secondary
failures without replacing the original category.

## Evidence and retirement

Before mutation, the guard checks genuine procfs, calling-thread visibility,
nsfs magic and namespace types, and Linux UAPI initial user/PID namespace inode
numbers (`4026531837` and `4026531836`). Identity-looking self maps are **not**
accepted as proof. This follows the actual NMRPeak/NMRNet namespace guard rather
than inventing a container-local identity fallback. Unavailable evidence fails
closed, including insufficient access for a non-root caller.

The explicit context name is resolved once using the standard Docker context
store (including `DOCKER_CONFIG` when set) to a local Unix endpoint. Subsequent calls use an
empty private Docker client configuration and retain/check socket identity,
daemon ID, security options, root directory, driver and OS type. Ambient engine
selection, TLS and proxy settings do not affect those calls. Each Docker command
streams at most 64 KiB across stdout and stderr, with bounded cancellation and
client process-group retirement. Killing the client does not prove that a daemon
mutation did not happen. The fingerprint identifies the logical selected engine;
it does not prove uninterrupted dockerd process lifetime (socket activation or a
rootless proxy can survive daemon restart). Exact probe-process/map stability is
the relevant observation guard; real restart/proxy qualification remains unrun.

The probe's full container ID, exact ownership labels, name, immutable image,
user/userns configuration and fixed entrypoint/argv must agree. Maps are bound
to an exact argv token, live host PID, process start time and genuine procfs
identity. Both maps are reread and checked for malformed/overlapping/overflowing
ranges and incomplete requested mappings; observations and container identity
are repeated, including before retirement. A changed/exited probe invalidates
success even when its owned container can still be removed safely.

Only the full verified owned container ID is removed. Successful selected-engine
inventory establishes absence; the observed process must also have retired.
The operation never deletes by a name prefix, prunes, or removes unrelated
resources. A failed/uncertain retirement is not a successful mapping.

## Durable failure/recovery record

The journal parent must already exist, be owned by the effective caller, and
have no group/other permission bits (normally mode 0700). A new file is opened
exclusively, mode 0600, with no final-component symlink following. The parent and
append-only events are fsynced. Existing journals are never overwritten.

The prepared event contains token, exact name, engine identity, image and
configuration. A separate **create-intent / uncertain-outcome** event is durable
before dispatch, so a crash cannot leave a false known-not-created conclusion.
Identified and finished events add the full container ID and established state.
Keep the journal until the owning operator accepts its terminal evidence;
automatic expiry and deletion are deliberately absent.

States are `known-not-created`, `identified-resource`, `confirmed-retired`, and
`uncertain-outcome`. If create output is lost, exact token candidates are
inspected against all recorded ownership/configuration evidence before cleanup.
An empty immediate inventory after a possibly dispatched create is **uncertain**,
not evidence that nothing was created. A proved pre-dispatch failure remains
known-not-created. There is no second create attempt.

Unresolved failures return the retained journal path, token, and any exact ID.
This slice does not yet expose an automatic recovery CLI. Preserve that handle
for host-side operator review of the *same* engine and complete exact ownership
evidence; do not rerun creation, delete by token/name alone, or prune in an
attempt to clear uncertainty. The finite shell lifetime limits execution but
leaves a stopped container requiring exact retirement after an abrupt host/client
loss. Success records are point-in-time evidence and must not be reused after
engine, configuration, or relevant namespace changes.

## Validation

The source-local focused lane is intentionally runnable without a Docker daemon:

```sh
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=packages/provider-deploy/src \
  python3 -m unittest discover -s packages/provider-deploy/tests -v
```

It tests real bounded child processes and real `/proc` observations, synthetic
namespace failures, and recorded Docker command/lifecycle boundaries. The real
`/proc` observation tests replace only the initial-host guard in this nested test
container; they are **not** host namespace or engine qualification. The separate
actual guard rejects this environment before Docker mutation.

Before consumer migration, qualify the maintained operation on selected local
rootful **and** rootless engines, with a non-root host caller, requested multi-row
maps, exact retirement, cancellation/timeouts, and nested-namespace rejection.
Record failures and unrun cases separately. Then demonstrate NMRPeak and NMRNet
calling the shared operation before removing their local implementations.

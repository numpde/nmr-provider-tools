# API and CLI reference

## Raw rows and explicit mappings

Import these names from `linux_idmap`. Importing the root package loads only
the row-reading and arithmetic core; Docker is used only by the probe operation.

| API | Contract |
|---|---|
| `Identifier(value)` | An `int` subclass accepting integer IDs from `0` through `4294967294`. Booleans, nonintegers and out-of-range values raise `ValueError`. |
| `Identifier.of(text)` | Parse 1–10 ASCII decimal digits in the same ID range. Leading zeroes are accepted; signs and surrounding whitespace are not. |
| `IdRange(inside, outside, length)` | An immutable row containing three plain integers. |
| `IdRange.of(line)` | Parse a byte string containing exactly three integer columns; malformed rows raise `ValueError`. |
| `parse_map(raw)` | Return a one-pass iterator of `IdRange` values in reported order. Empty input yields no rows. Parsing errors occur when the affected row is consumed. |
| `read_maps(read_file)` | Call `read_file("uid_map")` and then `read_file("gid_map")`, capturing both byte strings immediately. Return `MapRows(uid_rows, gid_rows)` with one-pass row iterators. |
| `observe_process(pid)` | Read `/proc/PID/uid_map` and `gid_map` through `read_maps`. Require a positive integer PID; file errors propagate as `OSError`. |
| `IdMap(rows)` | Collect an iterable of rows into an independent, editable `rows` list. The caller supplies and maintains non-overlapping affine ranges. |
| `IdMap.translate(identifier)` | Return an `Identifier` in the mapping's outside coordinates. Invalid, reserved or unmapped inputs, and invalid or reserved outputs, raise `ValueError`. |

Raw row parsing preserves reported numeric values, including sentinel fields.
It is separate from usable-ID validation and from establishing a coordinate
system. `IdMap` collects ranges; it does not validate their global consistency.
Within a supplied range, translation is `outside + identifier - inside`.

### Row coordinates

For `/proc/PID/uid_map` and `gid_map`, the first column is an ID in the target
process's user namespace. The second column is reported relative to the
map-file opener's user namespace. When opener and target share a user namespace,
the second column instead refers to that namespace's parent. In the initial
namespace, the kernel reports the identity range `0 0 4294967295`.

Linux uses `4294967295` for an unmapped outside ID. The kernel projects an
extent's first outside ID into the reporting namespace while retaining the
extent's length. Across arbitrary namespaces, that printed row need not describe
a contiguous affine mapping. Keep raw observations as rows unless their
coordinate relationship establishes the ranges you need. A process's self-map
supports conversion to its immediate parent; the host-qualified operations
below establish initial-namespace coordinates.

See [user_namespaces(7)](https://man7.org/linux/man-pages/man7/user_namespaces.7.html)
and the [kernel's map emission code](https://github.com/torvalds/linux/blob/v6.17/kernel/user_namespace.c#L610-L650).

## Existing-process host observation

```python
from linux_idmap.process import IdentityError, ProcessIdentity, ProcessObservation, observe_host_process
```

`observe_host_process(pid) -> ProcessObservation` requires Linux, genuine procfs
that exposes the calling process, and a reader in the initial user namespace.
The PID belongs to the caller's visible procfs; this operation does not require
the initial PID namespace.

The result contains `identity`, `uid_map` and `gid_map`. `identity` records the
PID, process start time and pinned procfs directory identity. Both maps are
reusable `IdMap` values. The observation record is frozen; each map retains its
normal editable `rows` list.

Observation pins the process directory, compares complete UID/GID samples and
checks reader and process identity before returning. The two map files are read
separately. The result is a checked observation, not a lock on the process's
future state.

Failed host or process checks raise `IdentityError`. Its `category` distinguishes
`host-precondition` from `process-identity`; its message describes the refusal.

## Local-image probe

```python
from linux_idmap.probe import (
    Bounds, ProbeFailure, ProbeRequest, ProbeResult, ResourceState,
    StepError, probe_identity,
)
```

### Request and time bounds

`ProbeRequest` has five required fields and two optional fields:

| Field | Meaning |
|---|---|
| `endpoint` | The explicitly selected local Docker Unix endpoint, such as `unix:///run/docker.sock`. |
| `image` | An image identifier already present on that engine. The operation resolves it locally and creates the probe from its image ID. |
| `interpreter` | Absolute path to Python inside the image, executable by the selected user. |
| `user_uid`, `user_gid` | The probe's numeric container identity; normalized to `Identifier` values. |
| `userns_mode` | `"default"` uses the engine's configured mapping; `"host"` requests Docker's host user-namespace mode. Default: `"default"`. |
| `bounds` | A `Bounds` value. Default: `Bounds()`. |

The image must have no declared volumes. The caller must be in the initial user
and PID namespaces, with access to the local engine and its processes in procfs.
The operation selects neither a Docker context nor an image on the caller's behalf.

`Bounds` uses seconds:

| Field | Default | Scope |
|---|---:|---|
| `operation_seconds` | 30 | Engine connection, creation, startup and map acquisition. |
| `command_seconds` | 5 | Each Docker client exchange, also limited by its phase's remaining time. |
| `cleanup_seconds` | 15 | Container removal, process-retirement checks and final engine verification. |
| `lifetime_seconds` | 90 | The finite sleep lifetime of the probe's Python process. |

The first three fields accept finite positive integers or floats up to 3600.
`lifetime_seconds` requires an integer from 1 through 3600. Acquisition and cleanup
have separate budgets; the acquisition limit is not a total call deadline.
Invalid request fields or bounds raise `StepError` during construction, before
engine contact. `StepError.category` is `invalid-policy` for these refusals.

### Operation and result

`probe_identity(request, *, cancel=None, executable="docker") -> ProbeResult`
is synchronous. `executable` selects the Docker client program. An optional
`cancel` object provides `is_set() -> bool`; the operation samples it during
acquisition and before delivering a result. Cancellation requests cleanup before
reporting failure.

Every probe replaces the image entrypoint with the selected interpreter, using
`-I -S -B` to isolate Python startup. It runs without networking, with a read-only
root filesystem, all capabilities dropped and no-new-privileges. Ownership and
configuration are checked before starting the container and during retirement.

The result contains host-coordinate `uid_map` and `gid_map` values and the retired
`container_id`. `ProbeResult.as_dict()` returns `ok`, both maps as lists of
`{inside, outside, length}` rows, the container ID and the state
`confirmed-retired`. The Python API returns maps; callers translate the IDs they
need after the operation succeeds.

The observed engine and user-namespace policy belong to this probe run. The
caller keeps the subsequent deployment consistent with them and owns all file,
ACL and permission changes.

### Failures and resource state

An operation failure raises `ProbeFailure`, which carries:

- `problem`: the primary `Problem(category, message)`.
- `retirement`: `Retirement(state, container_id, problems)`, including subsequent
  problems in execution order.
- `handle`: a `ProbeHandle` with the selected endpoint, operation token, and known
  container and daemon IDs, or `None` when no handle was established.

`ProbeFailure.as_dict()` exposes these fields as a JSON-serializable diagnostic
with `ok: false`. Use that diagnostic for operator reporting rather than raw
chained exceptions or Docker output. Failure ordering is acquisition, retirement,
client close, then the final cancellation check.

| `ResourceState` member | Serialized value | Meaning |
|---|---|---|
| `NOT_CREATED` | `known-not-created` | No probe container was created by the operation. |
| `UNRESOLVED` | `uncertain-outcome` | Creation or retirement remains unresolved. Use the handle to reconcile the owned resource before retrying. |
| `RETIRED` | `confirmed-retired` | Container absence, retirement of tracked processes and engine continuity were confirmed. |

Resource state describes cleanup, not measurement success. A `ProbeFailure` with
`RETIRED` remains a failure and supplies no maps. A lost removal reply can be
reconciled through confirmed absence and retirement; cancellation and independent
safety or client-cleanup failures remain failures.

## Command-line interface

```sh
linux-idmap-probe --endpoint unix:///run/docker.sock \
  --image my-image:local --interpreter /usr/bin/python3 \
  --user-uid 1000 --user-gid 1000 \
  --uid 0 --uid 1000 --gid 0 --gid 1000 --values
```

The first five options are required and match `ProbeRequest`. Optional
`--userns default|host` selects the namespace policy; omission uses `default`.
`--timeout SECONDS` overrides only `operation_seconds`. The command uses `docker`
from `PATH` and handles SIGINT, SIGTERM and SIGHUP as cancellation requests.

Repeat `--uid` and `--gid` to select up to 512 IDs in total. These are translation
requests, separate from `--user-uid` and `--user-gid`, which select the probe's
execution identity. All projections must succeed before output is emitted.

Without `--values`, stdout contains one JSON object. Success includes the
`ProbeResult.as_dict()` fields plus `mapped_uids` and `mapped_gids`; failure uses
`ProbeFailure.as_dict()`. With `--values`, successful stdout is one line of
space-separated decimal IDs: UIDs in their requested order, then GIDs in their
requested order. Failure leaves stdout empty and writes the JSON diagnostic to
stderr. With no projection options, the arrays are empty and successful
`--values` output is a blank line.

| Exit status | Meaning |
|---|---|
| `0` | Successful observation and requested projections, or `--help`. |
| `1` | A settled operation or projection failure. |
| `2` | Invalid or missing arguments, or invalid request policy. |
| `3` | An unresolved resource outcome; this takes precedence over cancellation. |
| `130` | Cancellation reported as the primary failure with a settled resource state. |

Check the exit status before consuming output. In particular, JSON mode reports
failures on stdout, and `confirmed-retired` alone does not indicate success.

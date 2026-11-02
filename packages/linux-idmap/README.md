# linux-idmap

Read process UID/GID map rows, translate IDs to host coordinates, or observe a
local Docker image before deploying it. The package uses Python's standard library.

Start with the [complete runnable examples](examples/README.md).

## Read a process's map rows

```python
import os
from linux_idmap import observe_process

maps = observe_process(os.getpid())
for row in maps.uid_rows:
    print(row.inside, row.outside, row.length)
```

`parse_map`, `read_maps` and `observe_process` return one-pass generators
of immutable named `IdRange` rows (grouped in `MapRows` for UID/GID pairs).
`read_maps` and `observe_process` read both files immediately; their rows are
parsed during iteration. The UID and GID files are sampled independently.

### Row coordinates

Outside IDs are reported relative to the map-file opener's user namespace.
For an unmapped outside ID, Linux reports `4294967295`. The kernel
projects only an extent's first outside ID into the reporting namespace, leaving
its length unchanged. That projection need not remain contiguous. When opener
and target share a user namespace, outside IDs refer to its parent (or the
initial namespace itself). See the
[kernel emission code](https://github.com/torvalds/linux/blob/v6.17/kernel/user_namespace.c#L610-L650)
and [boundary scenarios](tests/test_map_boundary.py).

`parse_map(bytes)` preserves reported rows, including empty maps and sentinel
fields. Each row is parsed by `IdRange.of(line)` as it is consumed, requiring
exactly three integer columns and raising `ValueError` for malformed rows.

### Explicit affine conversion

```python
from linux_idmap import IdMap, IdRange

mapping = IdMap([IdRange(inside=0, outside=1000, length=2)])
print(mapping.translate(1))  # 1001
```

`IdMap(rows)` copies the supplied iterable into an editable list. The rows must
describe non-overlapping affine ranges in the desired outside coordinates. For
example, `IdRange(0, 1000, 2)` maps IDs 0 and 1 to 1000 and 1001. A process
reading its own map can convert to its immediate parent's coordinates.

`Identifier(1000)` validates a numeric UID/GID; `Identifier.of("1000")` parses
ASCII decimal text. It remains an integer for arithmetic, formatting and JSON.
Raw kernel columns stay plain integers because sentinel fields are valid evidence.
The CLI and probe request use the same usable-ID validation.

`translate` returns an `Identifier` and raises `ValueError` for noninteger,
reserved or unmapped inputs, or reserved or out-of-range outputs.

## Translate an existing process's IDs to host IDs

```python
import os
from linux_idmap.process import observe_host_process

maps = observe_host_process(os.getpid())
print(maps.uid_map.translate(os.getuid()), maps.gid_map.translate(os.getgid()))
```

`observe_host_process(pid)` returns a `ProcessObservation` with `uid_map`,
`gid_map` and the observed `ProcessIdentity`. It requires the initial user
namespace and interprets the PID in the caller's visible procfs. It checks the
filesystem, pins the process directory, and compares process and map samples.
A failed check raises `IdentityError`. Changes that reverse between samples can
go undetected.

## Observe a local image before deployment

When the application does not exist yet, `probe_identity` creates an owned,
finite-lived Python probe in an **already local** image, observes its maps, and
returns them only after confirmed retirement:

```python
from linux_idmap.probe import ProbeRequest, probe_identity

result = probe_identity(ProbeRequest(
    endpoint="unix:///run/docker.sock",
    image="approved-image:local",
    interpreter="/usr/bin/python3",
    user_uid=1000,
    user_gid=1000,
    confine=True,
))
print(result.uid_map.translate(1000), result.gid_map.translate(1000))
```

The caller selects the local Unix endpoint, image, interpreter, identity and
user-namespace mode. Docker host-PID interpretation requires the initial PID
namespace as well as the initial user namespace. Choose a local image without
declared volumes. The probe replaces its entrypoint with Python, using
`-I -S -B` to skip startup hooks.

`Bounds` and `ProbeRequest` reject invalid policy with
`StepError` at construction, before any engine contact.
The request keeps normalized `Identifier` values for its UID and GID. Once an
operation starts, failures are reported as `ProbeFailure` with the resource's
retirement state. Import these types, `Bounds`, and `ResourceState` from
`linux_idmap.probe`.

The result describes the observed probe run. Normal exit after acquisition is
allowed. Cleanup uses the process identities recorded during observation. Its
required ownership inspection can still
expose an unrequested replacement run or an inconsistent PID, which prevents map
delivery even if cleanup succeeds. Every observed process identity remains a
retirement obligation. An owned reported PID that could not be identified must
be absent before cleanup is confirmed. The caller must keep its later deployment
policy consistent with the probe.

A lost or nonzero removal reply can be reconciled by confirmed container absence,
retirement of every tracked process, and unchanged engine identity. Only those
reply failures are discarded; cancellation, client-cleanup and safety failures
still prevent map delivery.

Failures are reported in execution order: measurement, retirement, client close,
then a final cancellation check. Cancellation waits for cleanup and returns a
failure.

Each Docker command owns a private process group. The transport bounds combined
stdout/stderr, checks cancellation, and signals the group before reaping its
leader—even when that leader has already exited successfully. Keeping the leader
waitable prevents its numeric group ID from being reused before signaling.
The caller must not auto-reap children or let another waiter consume this child;
lost wait ownership stops group signaling and reports a cleanup failure.
Descendants that leave the group are outside this supervision contract.

`confine=True` explicitly selects no networking, a read-only root filesystem,
dropped capabilities and no-new-privileges, and refuses inspected privilege,
device or mount drift before start.

## Translate IDs from the shell

```sh
linux-idmap-probe --endpoint unix:///run/docker.sock \
  --image approved-image:local --interpreter /usr/bin/python3 \
  --user-uid 1000 --user-gid 1000 --confine \
  --uid 0 --uid 1000 --gid 0 --gid 1000 --values
```

`--values` prints all requested UIDs, then all requested GIDs, after the operation and every
projection succeed. Cancellation observed before output vetoes success, including
a signal received during projection. In `--values` mode, failures leave stdout
empty and put a redacted structured diagnostic on stderr. `ProbeFailure.as_dict()`
provides the same operator reference: endpoint, daemon, operation token and known container ID.
Policy refusals use the same failure shape, with known-not-created state, null
container/handle, and no secondary problems. For an unresolved outcome, use the
reported engine and ownership reference to reconcile the resource before retrying.

## Build and install

Use Linux and Python 3.11+ with `venv`/`pip`. From the repository root:

```sh
cd packages/linux-idmap
build_env=$(mktemp -d)
python3 -m venv "$build_env"
"$build_env/bin/python" -m pip wheel --no-deps --wheel-dir dist . &&
  rm -r "$build_env"
python3 -m venv "$HOME/.local/share/linux-idmap"
"$HOME/.local/share/linux-idmap/bin/python" -m pip install \
  --force-reinstall --no-index --no-deps dist/linux_idmap-0.3.0a4-py3-none-any.whl
"$HOME/.local/share/linux-idmap/bin/linux-idmap-probe" --help
```

The build fetches the pinned backend declared in `pyproject.toml`; the installed
package has no third-party runtime dependencies. Use the installed environment's
Python, or put its `bin` directory on `PATH`, for host-side callers.

## Tests and qualification

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

The suite includes a physically isolated two-file core, real tokenless Linux
processes, genuine procfs coherence checks, real subprocess timeout/cancellation
and descendant-pipe scenarios, and deterministic Docker lifecycle fault tests.
Host-positive tests in a nested user namespace substitute only the qualification
evidence and are labeled as such. Real Docker engine/user-namespace scenarios
remain an adoption gate, including rootless/remapped engines and runtime confinement.

Provider-specific integrations live **outside this distribution**, in
[`integrations/linux-idmap`](../../integrations/linux-idmap). They opt into the
managed probe with confinement. Implementation details and scenario references
are in [DESIGN.md](DESIGN.md).

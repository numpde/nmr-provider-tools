# linux-idmap

Read a Linux process's UID/GID map rows and translate IDs through affine ranges.
The [core](src/linux_idmap/core.py) uses Python's standard library. Optional
add-ons check host coordinates and observe a temporary Docker probe.

```python
from linux_idmap import observe_process

maps = observe_process(pid)
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
from linux_idmap import IdMap

# rows describe affine ranges in the desired outside coordinates
outside_uid = IdMap(rows).translate(1000)
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

## Explicit add-ons

Add-ons are imported separately. Their file readers supply
**`read_file(name) -> bytes`** to the core parser; `read_maps` requests `uid_map`
and `gid_map`. See the [operations and their scenario tests](ADDONS.md).

```python
from linux_idmap.addons.observation import observe_host_process

maps = observe_host_process(pid)
host_uid = maps.uid_map.translate(1000)
```

This adds genuine procfs, bounded reads, pinned process identity and repeated
reader/process/map checks, plus initial-user-namespace qualification. The qualified
observation and managed probe return `IdMap` values in host coordinates.

The lower-level `coherent_process(pid)` provides a checked file reader.
`consistent_maps(read_file)` parses and compares two complete samples inside
the scope and returns raw rows. `qualified_process(pid)` also checks the
reader's initial user namespace. Use values captured inside a scope only after
successful exit. These checks detect differences
between samples; changes that reverse between checks can go undetected.

### A local image before deployment

When the application does not exist yet, the Docker add-on creates an owned,
finite-lived Python probe in an **already local** image, observes its maps, and
returns them only after confirmed retirement:

```python
from linux_idmap.addons.probe import ProbeRequest, probe_identity

result = probe_identity(ProbeRequest(
    endpoint="unix:///run/docker.sock",
    image="approved-image:local",
    interpreter="/usr/bin/python3",
    user_uid=1000,
    user_gid=1000,
    confine=True,
))
host_uid = result.uid_map.translate(1000)
```

The caller selects the local Unix endpoint, image, interpreter, identity and
user-namespace mode. Docker host-PID interpretation requires the initial PID
namespace as well as the initial user namespace. Choose a local image without
declared volumes. The probe replaces its entrypoint with Python, using
`-I -S -B` to skip startup hooks.

`Bounds` and `ProbeRequest` reject invalid policy with
`linux_idmap.addons.model.StepError` at construction, before any engine contact.
The request keeps normalized `Identifier` values for its UID and GID. Once an
operation starts, failures are reported as `ProbeFailure` with the resource's
retirement state.

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
device or mount drift before start. Its module is imported when selected.

### Shell projection add-on

```sh
linux-idmap-probe --endpoint unix:///run/docker.sock \
  --image approved-image:local --interpreter /usr/bin/python3 \
  --user-uid 1000 --user-gid 1000 --confine \
  --uid 0 --uid 1000 --gid 0 --gid 1000 --values
```

The module entry point is `python -m linux_idmap.addons.cli`.
`--values` prints all requested UIDs, then all requested GIDs, after the operation and every
projection succeed. Cancellation observed before output vetoes success, including
a signal received during projection. In `--values` mode, failures leave stdout
empty and put a redacted structured diagnostic on stderr. `ProbeFailure.as_dict()`
provides the same operator reference: endpoint, daemon, operation token and known container ID.
Policy refusals use the same failure shape, with known-not-created state, null
container/handle, and no secondary problems. For an unresolved outcome, use the
reported engine and ownership reference to reconcile the resource before retrying.

## Build and install this candidate

Use Linux and Python 3.11+ with `venv`/`pip`. From the repository root:

```sh
cd packages/linux-idmap
build_env=$(mktemp -d)
python3 -m venv "$build_env"
"$build_env/bin/python" -m pip wheel --no-deps --wheel-dir dist . &&
  rm -r "$build_env"
python3 -m venv "$HOME/.local/share/linux-idmap"
"$HOME/.local/share/linux-idmap/bin/python" -m pip install \
  --force-reinstall --no-index --no-deps dist/linux_idmap-0.3.0a3-py3-none-any.whl
"$HOME/.local/share/linux-idmap/bin/linux-idmap-probe" --help
```

The build fetches the pinned backend declared in `pyproject.toml`; the installed
package has no third-party runtime dependencies. Use the installed environment's
Python, or put its `bin` directory on `PATH`, for host-side callers. This builds an
unreleased candidate.

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

Provider-specific candidate migrations live **outside this distribution**, in
[`integrations/linux-idmap`](../../integrations/linux-idmap). They opt into the
probe and confinement add-ons. In `0.3.0a3`, raw callers consume row generators;
`IdMap(rows)` and qualified add-on results provide `.rows` and `.translate`.

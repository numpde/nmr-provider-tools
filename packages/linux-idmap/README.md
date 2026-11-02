# linux-idmap

Read a Linux process's UID/GID map rows. The complete core is
[`core.py`](src/linux_idmap/core.py): one page of logic plus plain record
definitions, with only the root re-export file alongside it.
It uses only Python's standard library; Docker is not required.

```python
from linux_idmap import observe_process

maps = observe_process(pid)
for row in maps.uid_rows:
    print(row.inside, row.outside, row.length)
```

`parse_map`, `read_maps` and `observe_process` return one-pass generators
of immutable named `IdRange` rows (grouped in `MapRows` for UID/GID pairs).
`MapRows.uid_rows` and `.gid_rows` contain reported rows, not translating maps.
Observation samples two files independently; it does not claim host coordinates, a live/stable
process, an atomic UID/GID snapshot, or an ID conversion operation.

**Reported rows are not necessarily a usable conversion table.** Depending on
the map-file opener's user namespace, Linux can report `4294967295` for an unmapped
outside ID. Even ordinary-looking numbers can lose information: the kernel
projects only an extent's first outside ID into the reporting namespace, leaving
its length unchanged. That projection need not remain contiguous. When opener
and target share a user namespace, outside IDs refer to its parent (or the
initial namespace itself). See the
[kernel emission code](https://github.com/torvalds/linux/blob/v6.17/kernel/user_namespace.c#L610-L650)
and [boundary scenarios](tests/test_map_boundary.py).

`parse_map(bytes)` preserves reported rows, including empty maps and sentinel
fields. Each row is parsed by `IdRange.of(line)` as it is consumed, requiring
exactly three integer columns and raising `ValueError` for malformed rows.
It is not a validator for arbitrary imported mappings, and numeric validation cannot recover missing namespace context.

### Explicit affine conversion

```python
from linux_idmap import IdMap

# Only when the caller knows these rows are affine in the intended coordinates:
outside_uid = IdMap(rows).translate(1000)
```

`IdMap(rows)` is a caller assertion, not validation or namespace qualification.
It copies the supplied rows into a reusable list, including when given a generator.
The rows must describe usable affine ranges in known outside coordinates.
For example, a process reading its own map can convert to its immediate parent's
coordinates without claiming host coordinates. The boundary tests exercise this
in a real isolated child user namespace.

`Identifier(1000)` validates a numeric UID/GID; `Identifier.of("1000")` parses
ASCII decimal text. It remains an integer for arithmetic, formatting and JSON.
Raw kernel columns stay plain integers because sentinel fields are valid evidence.
The CLI and probe request use the same usable-ID validation.

`translate` returns an `Identifier`, rejects noninteger, reserved and unmapped
input IDs, and refuses reserved or out-of-range output IDs. That last guard is not a table validator:
it cannot detect a plausible but incorrect answer from a fragmented reader.
Parsing never automatically constructs an `IdMap`.

## Explicit add-ons

An add-on is a separately imported source/context or guarantee-refining
operation. It composes through **`read_file(name) -> bytes`**, where `read_maps`
requests only `uid_map` and `gid_map`. There is one parser, not a parallel
implementation per backend. No registry, mixin hierarchy or implicit discovery
is needed. See the [contracts and necessity scenarios](ADDONS.md).

```python
from linux_idmap.addons.observation import observe_host_process

maps = observe_host_process(pid)
host_uid = maps.uid_map.translate(1000)
```

This adds genuine procfs, bounded reads, pinned process identity and repeated
reader/process/map checks, plus initial-user-namespace qualification. The qualified
observation and managed probe construct the same neutral `IdMap` value, with
its host-coordinate precondition established by these source checks. They do
not introduce a second mapping type.

Reading through the lower-level `coherent_process(pid)` checked source or
`consistent_maps(read_file)` still yields raw rows. It consumes and compares
complete list snapshots inside the checked scope, before any deferred parsing
error or scope-exit failure can release a qualified result. `qualified_process(pid)`
additionally checks the reader's initial user namespace; values read inside a
scope must not be used if its exit fails. None promises an atomic snapshot or
immunity to changes that occur and reverse between checks. Observing an existing
process never creates or stops it.

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
namespace as well as the initial user namespace. Images declaring implicit
volumes are refused: this operation owns a container/process, not persistent
volume resources. Images are never pulled and application entrypoints are not
run. Python uses `-I -S -B` to avoid startup hooks.

`Bounds` and `ProbeRequest` reject invalid policy with
`linux_idmap.addons.model.StepError` at construction, before any engine contact.
The request keeps normalized `Identifier` values for its UID and GID. Once an operation starts, failures are reported as `ProbeFailure`
with the resource's retirement state; neither failure type carries usable maps.

The result describes the observed probe run. Normal exit after acquisition is
allowed; cleanup never rereads maps. Its required ownership inspection can still
expose an unrequested replacement run or an inconsistent PID, which prevents map
delivery even if cleanup succeeds. Every observed process identity remains a
retirement obligation. An owned reported PID that could not be identified must
be absent before cleanup is confirmed. The caller must keep its later deployment
policy consistent with the probe; these maps do not predict a future run after policy
or engine changes.

A lost or nonzero removal reply can be reconciled by confirmed container absence,
retirement of every tracked process, and unchanged engine identity. Only those
reply failures are discarded; cancellation, client-cleanup and safety failures
still prevent map delivery.

Failures are reported in execution order: measurement, retirement, client close,
then a final cancellation check. A later close error never hides an earlier
retirement failure. Cancellation does not interrupt cleanup or permit map output.

Each Docker command owns a private process group. The transport bounds combined
stdout/stderr, checks cancellation, and signals the group before reaping its
leader—even when that leader has already exited successfully. Keeping the leader
waitable prevents its numeric group ID from being reused before signaling.
The caller must not auto-reap children or let another waiter consume this child;
known lost wait ownership is a failure, never permission to signal a reused ID.
Descendants that leave the group are outside this supervision contract.

`confine=True` explicitly selects no networking, a read-only root filesystem,
dropped capabilities and no-new-privileges, and refuses inspected privilege,
device or mount drift before start. It is **not** a claim that simulated-engine
tests qualify Docker's runtime isolation. Fixed memory/PID/CPU quotas are not
part of the API. Unselected confinement is not imported.

### Shell projection add-on

```sh
linux-idmap-probe --endpoint unix:///run/docker.sock \
  --image approved-image:local --interpreter /usr/bin/python3 \
  --user-uid 1000 --user-gid 1000 --confine \
  --uid 0 --uid 1000 --gid 0 --gid 1000 --values
```

The explicit module entry point is `python -m linux_idmap.addons.cli`.
There is no root CLI or `identity` subcommand. `--values` prints all requested
UIDs, then all requested GIDs, only after the entire operation and every
projection succeed. Cancellation observed before output vetoes success, including
a signal received during projection. In `--values` mode, failures leave stdout
empty and put a redacted structured diagnostic on stderr. `ProbeFailure.as_dict()` provides the same minimal
operator reference: endpoint, daemon, operation token and known container ID.
Policy refusals use the same failure shape, with known-not-created state, null
container/handle, and no secondary problems. An uncertain outcome is not
permission to repeat a mutation or delete a similarly named resource.

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
unreleased candidate, not a published release or real-engine qualification.

## Tests and limitations

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
```

The suite includes a physically isolated two-file core, real tokenless Linux
processes, genuine procfs coherence checks, real subprocess timeout/cancellation
and descendant-pipe scenarios, and deterministic Docker lifecycle fault tests.
Host-positive tests in a nested user namespace substitute only the qualification
evidence and are labeled as such. Real Docker engine/user-namespace scenarios
remain unqualified; do not deploy this candidate on the strength of unit tests.

Provider-specific candidate migrations live **outside this distribution**, in
[`integrations/linux-idmap`](../../integrations/linux-idmap). They opt into the
probe and confinement add-ons. No legacy branded API compatibility shim is
included in this unreleased `0.3.0a3` candidate. Raw callers now consume row generators, not
automatically translating `IdMap` wrappers. Explicit `IdMap(rows)` construction
and qualified add-on results retain `.rows`, `.translate`, and named-row JSON
output.

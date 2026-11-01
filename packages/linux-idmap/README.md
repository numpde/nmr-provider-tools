# linux-idmap

Read a Linux process's UID/GID map rows. The complete core is
[`core.py`](src/linux_idmap/core.py): one page, plus the root re-export file.
It uses only Python's standard library; Docker is not required.

```python
from linux_idmap import observe_process

maps = observe_process(pid)
for row in maps.uid_map:
    print(row.inside, row.outside, row.length)
```

The core returns immutable, ordered tuples of named `IdRange` rows. It samples
two files independently; it does not claim host coordinates, a live/stable
process, an atomic UID/GID snapshot, or an ID conversion operation.

**Reported rows are not necessarily a usable conversion table.** Depending on
the reader's user namespace, Linux can report `4294967295` for an unmapped
outside ID. Even ordinary-looking numbers can lose information: the kernel
projects only an extent's first outside ID into the reporting namespace, leaving
its length unchanged. That projection need not remain contiguous. When reader
and target share a user namespace, outside IDs refer to its parent (or the
initial namespace itself). See the
[kernel emission code](https://github.com/torvalds/linux/blob/v6.17/kernel/user_namespace.c#L610-L650)
and [boundary scenarios](tests/test_map_boundary.py).

`parse_map(bytes)` preserves reported rows, including empty maps and sentinel
fields. It eagerly requires exactly three integer columns per line, raising
`ValueError` for malformed rows. It is not a validator for arbitrary imported
mappings, and numeric validation cannot recover missing namespace context.

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
reader/process/map checks, plus initial-user-namespace qualification. Only this
qualified observation and the managed probe produce `HostMap` conversion
values. `HostMap(rows)` itself does not verify provenance; its constructor is
not a certificate. `translate` rejects noninteger, reserved and unmapped input
IDs with `ValueError`.

Reading through the lower-level `coherent_process(pid)` checked source or
`consistent_maps(read_file)` still yields raw rows. `qualified_process(pid)`
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
projection succeed. Failures leave stdout empty and put a redacted structured
diagnostic on stderr. `ProbeFailure.as_dict()` provides the same minimal
operator reference: endpoint, daemon, operation token and known container ID.
An uncertain outcome is not permission to repeat a mutation or delete a
similarly named resource.

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
included in this unreleased `0.3.0a2` candidate. The raw `IdMap` wrapper and its conversion
method were removed; raw callers now iterate row tuples. Qualified add-on
results retain `.rows`, `.translate`, and named-row JSON output.

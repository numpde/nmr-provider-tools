# linux-idmap

Read Linux UID/GID maps, translate IDs between namespaces, and find the host IDs
used by a local Docker image. `linux-idmap` provides a Python API and the
`linux-idmap-probe` command. It uses the standard library and requires Python
3.11 or later.

Choose the operation that matches the information you have:

| Task | Operation |
|---|---|
| Inspect the rows reported for a running process | `linux_idmap.observe_process(pid)` |
| Translate an ID through known affine ranges | `linux_idmap.IdMap(rows).translate(id)` |
| Obtain checked host-coordinate maps for a running process | `linux_idmap.process.observe_host_process(pid)` |
| Obtain host-coordinate maps before a workload exists | `linux_idmap.probe.probe_identity(request)` or `linux-idmap-probe` |

The package observes identities. Your application selects the process or image
and decides how to use the translated IDs for ownership, permissions or deployment.

## Install

From the repository root, install into a host Python environment:

```sh
python3 -m venv "$HOME/.local/share/linux-idmap"
"$HOME/.local/share/linux-idmap/bin/python" -m pip install --no-deps ./packages/linux-idmap
export PATH="$HOME/.local/share/linux-idmap/bin:$PATH"
linux-idmap-probe --help
```

Installation from source fetches the pinned build backend in `pyproject.toml`.
The installed package has no third-party runtime dependencies. Docker probing
also requires the Docker CLI and access to the selected local Unix socket.

## Inspect a process's map rows

```python
import os
from linux_idmap import observe_process

maps = observe_process(os.getpid())
for row in maps.uid_rows:
    print(row.inside, row.outside, row.length)
```

Each row contains an inside start, an outside start and a count. The outside
coordinate system depends on the namespace of the process reading the map;
reading your own map gives the parent namespace's coordinates. Use the host
observation below when you need host IDs. See [row coordinates](REFERENCE.md#row-coordinates)
for the initial-namespace case and the distinction between reported rows and
ranges suitable for arithmetic.

## Translate known ranges

```python
from linux_idmap import IdMap, IdRange

mapping = IdMap([IdRange(inside=0, outside=1000, length=2)])
print(mapping.translate(1))  # 1001
```

`IdMap` collects its input into an editable list. Supply non-overlapping affine
ranges in the coordinate system you intend to use. `translate` returns an
integer-like `Identifier`; invalid or unmapped IDs raise `ValueError`.

## Find a running process's host IDs

```python
import os
from linux_idmap.process import observe_host_process

observation = observe_host_process(os.getpid())
print(observation.uid_map.translate(os.getuid()))
print(observation.gid_map.translate(os.getgid()))
```

Run this operation in the host's initial user namespace with readable `/proc`.
It checks the reader, process identity and repeated map samples before returning
reusable mappings. The PID is interpreted in the caller's visible procfs.

## Find an image's host IDs before deployment

```python
from linux_idmap.probe import ProbeRequest, probe_identity

request = ProbeRequest(
    endpoint="unix:///run/docker.sock",
    image="my-image:local",
    interpreter="/usr/bin/python3",
    user_uid=1000,
    user_gid=1000,
)
result = probe_identity(request)
print(result.uid_map.translate(request.user_uid))
print(result.gid_map.translate(request.user_gid))
```

Select an image already present on the chosen engine, without declared volumes,
and a Python interpreter executable by the requested user. Run the caller in
the host's initial user and PID namespaces. Replace the example's endpoint,
image, interpreter and IDs with your deployment's values.

The operation creates a temporary Python process with no network, a read-only
root filesystem, dropped capabilities and no-new-privileges. It returns the maps
after confirming container and observed-process retirement. Use the same engine
and user-namespace policy for the later deployment.

Shell callers can request the translated IDs directly:

```sh
linux-idmap-probe --endpoint unix:///run/docker.sock \
  --image my-image:local --interpreter /usr/bin/python3 \
  --user-uid 1000 --user-gid 1000 --uid 1000 --gid 1000 --values
```

Successful `--values` output contains the requested UIDs followed by the requested
GIDs. On failure, stdout is empty and stderr contains structured JSON. The
[reference](REFERENCE.md#command-line-interface) describes all options, output
formats and exit statuses, including unresolved cleanup.

## Documentation and development

[Runnable examples](examples/README.md) cover all four operations.
[API and CLI reference](REFERENCE.md) describes types, errors, time bounds and
resource states. [Implementation design](DESIGN.md) describes module ownership
and the observation and retirement workflows.

From `packages/linux-idmap`, run the tests or build a wheel:

```sh
PYTHONPATH=src python -m unittest discover -s tests -v
python -m pip wheel --no-deps --wheel-dir dist .
```

Install a built wheel into the chosen environment with
`python -m pip install --no-index --no-deps /absolute/path/to/package.whl`, replacing
that path with the generated wheel's actual filename.

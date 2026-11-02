# Complete examples

These examples use `linux-idmap` version `0.3.0a4`.
Run them from `packages/linux-idmap` on Linux with Python 3.11+.
`PYTHONPATH=src` uses the checkout; omit it when using an installed package.

## Read your process's map rows

```sh
PYTHONPATH=src python examples/read_self.py
```

[`read_self.py`](read_self.py) prints `uid` or `gid`, then the inside start,
outside start and count for every row. Values depend on the process's namespace.
For example, an initial-namespace process may print:

```text
uid 0 0 4294967295
gid 0 0 4294967295
```

## Translate an ID through a known range

```sh
PYTHONPATH=src python examples/translate.py
```

[`translate.py`](translate.py) defines the whole mapping and prints `1001`.

## Find your process's host UID and GID

```sh
PYTHONPATH=src python examples/host_ids.py
```

[`host_ids.py`](host_ids.py) observes its own already-running Python process
and prints two integers: its host UID and GID. Run it in
the host's initial user namespace with readable `/proc`. A nested user namespace
produces `host-precondition: Map reader must be in the initial user namespace`
on stderr and exits unsuccessfully.

## Observe a local image before deployment

[`probe_image.py`](probe_image.py) takes five arguments: the selected local Docker
socket, an already-local image, its Python interpreter path, and the container
UID and GID. For example, if your local image is `my-image:local`, its interpreter
is `/usr/bin/python3`, and you want container user `1000:1000`:

```sh
PYTHONPATH=src python examples/probe_image.py \
  unix:///run/docker.sock my-image:local /usr/bin/python3 1000 1000
```

Replace those five arguments with your actual values. Run on the host in its
initial user and PID namespaces, with readable procfs, the Docker CLI on `PATH`
and socket access. The already-local image must have no declared volumes and
Python executable by the selected UID and GID. The example
selects confinement and prints the two translated host IDs after the probe is
retired. On failure it exits unsuccessfully; an operation failure prints JSON
with the retirement state and resource reference on stderr.

## Verification

The first three examples can run without Docker. This environment has no Docker
executable, so the image example has not been run against an engine here.

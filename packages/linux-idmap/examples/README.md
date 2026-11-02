# Runnable examples

These programs demonstrate the public Python API. [Install the package](../README.md#install),
then run them from this directory with that environment's Python. From a source
checkout, use the commands below from `packages/linux-idmap`; `PYTHONPATH=src`
selects the checkout instead of an installed package.

## Inspect a process's map rows

```sh
PYTHONPATH=src python examples/read_self.py
```

[`read_self.py`](read_self.py) prints `uid` or `gid`, followed by each row's
inside start, outside start and count. For a process in the initial user
namespace, the output is:

```text
uid 0 0 4294967295
gid 0 0 4294967295
```

The outside coordinates depend on the reader's namespace. See
[row coordinates](../REFERENCE.md#row-coordinates) before using raw rows for
translation.

## Translate an ID through known ranges

```sh
PYTHONPATH=src python examples/translate.py
```

[`translate.py`](translate.py) supplies the complete affine mapping and prints
`1001`. Reuse the resulting `IdMap` for additional translations.

## Find a running process's host UID and GID

```sh
PYTHONPATH=src python examples/host_ids.py
```

[`host_ids.py`](host_ids.py) observes its own Python process and prints its host
UID and GID. Run it in the initial user namespace with readable `/proc`.
A failed identity check prints its category and message to stderr and exits
unsuccessfully.

## Probe a local image before deployment

[`probe_image.py`](probe_image.py) takes five arguments: the selected local Docker
endpoint, an already-local image, its absolute Python interpreter path, and the
container UID and GID.

```sh
PYTHONPATH=src python examples/probe_image.py \
  unix:///run/docker.sock my-image:local /usr/bin/python3 1000 1000
```

Replace all five values with your deployment's choices. Run in the host's initial
user and PID namespaces, with readable procfs, the Docker CLI on `PATH` and access
to the selected socket. The image must have no declared volumes, and its Python
interpreter must be executable by the requested user.

The example prints the two host IDs after the confined probe has retired.
An operation failure prints its structured diagnostic to stderr and exits
unsuccessfully. The [failure reference](../REFERENCE.md#failures-and-resource-state)
explains how to distinguish a settled failure from an unresolved resource.

For shell automation, use `linux-idmap-probe --values` and its documented
[exit statuses](../REFERENCE.md#command-line-interface).

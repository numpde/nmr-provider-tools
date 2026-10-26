# NMR provider tools

This repository will own model-independent provider infrastructure: the shared
`provider_core` package, durable result delivery, and delivery deployment tooling.
Providers retain scientific input preparation, inference, model runners,
offerings and model configuration.

The current checkout owns its repository checks and their preparation workflow.
The provider package and delivery service have not been extracted yet.

Run `make help` to discover commands. `make tests` runs every current check;
`make tests/repository/quality` narrows the run to convention detectors and their
fixtures. `make tests/repository/runtime` verifies the actual checker container.
`make tests/repository/provisioning` runs preparation and cleanup fixtures.
`make tests/repository/command` prints a launch preview without contacting Docker;
the real launcher resolves the printed fingerprint tag to an admitted image ID.

The lane requires Linux/amd64, Bash, GNU coreutils and find, a non-root caller,
and Docker with BuildKit at `unix:///var/run/docker.sock`. Initialize a host with:

```sh
make checks/base-image/download
make checks/prepare WHEELHOUSE=/path/to/shared/cache NETWORK_INTERFACE=your-interface
make tests
```

The first command pulls the digest-pinned official Python base using Docker
daemon routing. This bootstrap is an explicit exception: the daemon pull cannot
bind to `NETWORK_INTERFACE`. The shared base also runs the acquisition proxy.
Subsequent artifact downloads bind both DNS and TCP to the selected interface.
When omitted interactively, the interface chooser shows adapters, virtual links
and link state without choosing a default. Noninteractive calls must select it
on the Make command line; an unavailable or explicitly empty value fails.

`WHEELHOUSE` selects an additive shared artifact cache, with no default location.
Absolute and relative paths are accepted; an omitted interactive value prompts
for a path. Ambient environment values do not authorize either selection.
The checker currently requires no Python wheels: its separately declared OS
artifact is GNU Make. Acquisition verifies pinned size and SHA-256 before
publication, preserves identical existing files and rejects conflicting bytes.
Unrelated cache files remain untouched. Failed download staging is retained
under the selected cache and its path is printed.

To reuse cached artifacts offline:

```sh
make checks/dependencies/check WHEELHOUSE=/path/to/shared/cache
make checks/image/build WHEELHOUSE=/path/to/shared/cache
```

The build captures source and verified dependency bytes in a private `/tmp`
directory, then builds with networking disabled. Prepared images live in Docker
under a tag derived from construction inputs, including their bytes and modes.
Checks verify the image label and execute its immutable ID. No checkout image
record is maintained. A missing or stale image fails with preparation guidance;
normal test targets never acquire dependencies or rebuild automatically.
Dependency verification and capture have 60-second client budgets; image builds
have a 900-second budget. Repository test execution has no imposed deadline.

Checks run without networking, credentials, an engine socket or writable source.
Public root entries are mounted read-only; private control and state directories
are excluded and represented only by names and types in a temporary inventory.
The checker receives a non-root UID/GID, a read-only root filesystem, no Linux
capabilities, no-new-privileges, two CPUs, 512 MiB memory, no swap, 64 PIDs and a
64 MiB non-executable temporary filesystem. An init process reaps terminated
test children. Containers share the host kernel;
these restrictions do not establish a hostile-code sandbox.

Offline checks and builds use private temporary inputs, isolated Docker client
settings and an operation label. Cleanup removes only their owned containers, verifies
their absence, and then deletes mounted inputs and client configuration.
Repeated signals do not interrupt bounded teardown; cleanup failure preserves
the original failure status. Unconfirmed creation/retirement or an unsuccessful
dispatched build retains inputs and reports recovery paths. Inspect the reported
label and retained files before removing them manually. Shared cache files and
prepared images survive cleanup. SIGKILL and host crashes cannot run exit traps.
Acquisition uses the imported transport's separate owner: exact create-output
and CID receipts identify its proxy and worker for removal. Unconfirmed client
or container retirement retains that transport's private state for inspection.

Repository findings may remain red. A missing tool, failed import or failed
container launch is a failed proof prerequisite, not a passing check or a
convention finding. Preserve detector rejection cases when correcting a port.
The [source manifest](tests/repository/port-sources.json) identifies donor bytes;
the [dispositions](tests/repository/port-dispositions.json) explain donor-specific
test methods replaced at the local boundary.

Ruff formatting is owned by the separate `ruffer` checkout. This repository
keeps its custom convention and import-order checks and does not install or
invoke Ruff. Ruffer currently provides single-file formatting lanes; this
separation does not claim repository-wide Ruff lint coverage.

The [initialization record](notes/002_checker_initialization_20261026.txt)
records the preparation precedents, dependency review and qualification.

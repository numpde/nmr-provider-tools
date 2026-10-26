# NMR provider tools

This repository will own model-independent provider infrastructure: the shared
`provider_core` package, durable result delivery, and delivery deployment tooling.
Providers retain scientific input preparation, inference, model runners,
offerings and model configuration.

The checkout owns the shared package, delivery runtime and host lifecycle, plus
repository checks and offline qualification. Package releases go to GitHub
Releases; this project does not publish to a Python package index.

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
make tests/repository
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

The package lane uses separately closed runtime and build wheel locks. Acquire
those third-party dependencies, then test the installed core without networking:

```sh
make packages/provider-core/download WHEELHOUSE=/path/to/shared/cache NETWORK_INTERFACE=your-interface
make tests/unit/provider-core tests/contract/provider-core tests/integration/provider-core WHEELHOUSE=/path/to/shared/cache
```

`make tests WHEELHOUSE=... SHARED_RUNTIME_IMAGE=sha256:...` includes repository
checks, all three installed-core lanes and the prepared shared-image tests. Package tests run from copied test files against a private installed wheel,
with no consumer checkout or scientific environment on the import path.

`make package/build WHEELHOUSE=... OUTPUT=...` captures clean committed HEAD and
writes the wheel, source distribution, complete hash installation lock, source
record and hash manifest into an existing empty caller-owned mode 0700 directory.
`make tests/package WHEELHOUSE=... ARTIFACTS=...` verifies those exact five files,
checks source and wheel completeness, rebuilds offline from the source archive,
and tests both installed wheels. Only successful qualification emits the exact
artifact hashes. Candidate artifacts are separate from the additive shared cache.
Consumers acquire the versioned wheel from GitHub Releases with its SHA-256,
then install its complete dependency lock offline.

Build the shared runtime from a qualified five-file release directory:

```sh
make delivery/image/build WHEELHOUSE=/path/to/shared/cache ARTIFACTS=/private/release
make tests/deployment SHARED_RUNTIME_IMAGE=sha256:the-built-image-id
```

The build installs the issued wheel and its complete lock offline. It includes
shared delivery and API/acquisition proxies, with no scientific provider source.
Both roles use `/opt/provider-core/environment/bin/python`. The build prints the
immutable image ID; provider deployments receive that ID explicitly.

Copy [the binding example](config/delivery.example.toml) to a private mode 0600
file. Set the installing UID, shared socket GID, API identity/trust, route and
queue allowances explicitly. One through sixteen bindings may share the host
service; providers select their own named binding. Each producer mounts only its
binding's IPC volume read-only and adds the configured socket GID. Providers own
scientific configuration, checkpoint initialization and handover retry pacing.

The host lifecycle requires `jq`, the existing Docker/Compose prerequisites and
one canonical private state root per daemon-global project:

```sh
mkdir -m 700 /private/delivery-state
make delivery/deployment/install DELIVERY_ROOT=/private/delivery-state DELIVERY_BINDINGS=/private/bindings.toml SHARED_RUNTIME_IMAGE=sha256:the-built-image-id
make delivery/deployment/up DELIVERY_ROOT=/private/delivery-state
make delivery/deployment/status DELIVERY_ROOT=/private/delivery-state
make delivery/deployment/logs DELIVERY_ROOT=/private/delivery-state
make delivery/deployment/down DELIVERY_ROOT=/private/delivery-state
```

Install freezes one bounded definition snapshot and grants only the selected API
credential, optional CA and external resolver files. It writes a sealed recipe
under the selected state root. Status and down use that recipe. Local API routes
attach the specified immutable network ID; mapped and external API proxies use
host networking and bind traffic to the explicit interface. The daemon has no
network and reaches the API only through its private proxy socket. These are the
shared publisher's routing grants; each provider owns its producer transport.

Before adopting an existing shared queue, inspect and drain it with its current
owner where possible, stop that owner's project, and retain its queue/IPC volumes.
Keep its project name, daemon UID/GID, API/provider identities and socket leaves.
Then install the complete binding definition here. This owner refuses existing
project containers; it does not stop another owner automatically. Queue startup
refuses omitted outstanding bindings. No result importer or science rerun is part
of this handoff. Ordinary down preserves all durable state. Per-root locking does
not serialize aliases selecting the same global project; use one state root.

For stopped delivery, `delivery/deployment/held` takes explicit `API_ORIGIN` and
`PROVIDER_REF` and lists retained metadata. `delivery/deployment/discard` additionally
requires the exact `EXECUTION_ATTEMPT_REF` and `BODY_SHA256`. Discard removes only a
matching held payload; it proves no API acceptance. Running delivery must be
stopped before either command opens its single-writer queue.

Authored release notes live in `notes/NNN_release_vX.Y.Z.md`, with one source line
per paragraph or list item. Commit the package version and its release note,
then prepare a reviewable release directory:

```sh
mkdir -m 700 /private/release-inputs
make publish/prepare TAG=vX.Y.Z WHEELHOUSE=/path/to/shared/cache OUTPUT=/private/release-inputs
make publish/release TAG=vX.Y.Z ARTIFACTS=/private/release-inputs
```

Preparation captures committed source, qualifies the exact five artifacts, and
retains build/test logs plus the authored note. Inspect those public inputs
before publication. The release target captures those same bytes, requires their
successful qualification hashes and unchanged source/note, checks the canonical
GitHub destination, and refuses an existing release. It pushes the tag, uploads a
draft, downloads and compares every artifact and both logs, then makes the draft
public. GitHub credentials remain in host orchestration. Failed or uncertain
publication retains its private receipts; inspect them before retrying. Published
assets are never replaced.

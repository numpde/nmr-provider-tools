# NMR provider tools

Shared infrastructure for NMR API providers: the `provider_core` Python package,
durable terminal-result delivery, and tools to build and operate the delivery
service. Core supplies model-independent signed API requests, protocol handling,
clients and lifecycle/recovery helpers. Providers own scientific input
preparation, inference, model runners, offerings and model configuration.

Source is public at [numpde/nmr-provider-tools](https://github.com/numpde/nmr-provider-tools).
Packages are distributed through [GitHub Releases](https://github.com/numpde/nmr-provider-tools/releases),
including [v0.2.0](https://github.com/numpde/nmr-provider-tools/releases/tag/v0.2.0),
rather than a Python package index. See the [package documentation](packages/provider-core/README.md)
for API boundaries and provenance.

## Use core in a provider

Pin the released wheel by version and SHA-256 together with its complete
installation lock. The lock includes core and every runtime dependency. Acquire
the wheel, lock and locked third-party wheels explicitly, then install from the
local cache in a fresh Python 3.12 or newer environment inside the provider's
container build or runtime:

```sh
python -m pip install --no-index --no-deps --only-binary=:all: \
  --find-links /path/to/cache --require-hashes \
  -r /path/to/provider_core-0.2.0-install.lock
```

The [v0.2.0 release note](notes/005_release_v0.2.0.md) records the exact wheel
hash and asset links. Consumer builds install those issued bytes offline;
they do not rebuild core or import a sibling checkout. Operational modules run
through container launchers that supply the package's container marker.

A provider hands exact complete/fail request bytes to `DeliveryClient` over its
configured Unix socket. A matching acknowledgement (ACK) means **durable local
enqueue**. Shared delivery then owns the pending payload, API publication and
retries. The ACK is not a remote API acceptance receipt.

While handover is unresolved, keep the exact request bytes available. Retire the
prior exchange and follow the client's retry disposition; a lost ACK can permit
replaying the identical request after retryable I/O has settled. Queue
backpressure pauses new work admissions until space is available; already-started
work can finish. A missing queue row alone does not prove that an earlier request
was never delivered. Providers own handover retry pacing
and compact control checkpoints; shared delivery owns durable pending result
storage. Provider integration should not add a second durable scientific-output
outbox or rerun inference to recover publication.

## Prepare a build host

The maintained commands require Linux/amd64, Bash, GNU Make, GNU coreutils and GNU
find, a non-root caller, and Docker with BuildKit at `unix:///var/run/docker.sock`.
Delivery lifecycle commands also require Docker Compose and `jq`.
Run `make help` for the full command list.

Acquisition and offline work are separate operations. Bootstrap the pinned
Python base image, acquire repository-checker artifacts, and acquire the closed
core runtime/build wheel sets:

```sh
make checks/base-image/download
make checks/dependencies/download WHEELHOUSE=/path/to/shared/cache NETWORK_INTERFACE=your-interface
make packages/provider-core/download WHEELHOUSE=/path/to/shared/cache NETWORK_INTERFACE=your-interface
```

The base-image pull uses Docker daemon routing and cannot bind to
`NETWORK_INTERFACE`. Later artifact acquisition binds DNS and TCP to the selected
interface. Pass `NETWORK_INTERFACE` and `WHEELHOUSE` on the Make command line;
interactive calls can prompt for omitted values. Environment values do not
select either. The wheelhouse is an additive shared cache with no default
location. Acquisition verifies declared sizes and SHA-256 hashes, preserves
identical cached files and rejects conflicting bytes.

With the base image and artifacts already present, prepare the checker offline:

```sh
make checks/dependencies/check WHEELHOUSE=/path/to/shared/cache
make checks/image/build WHEELHOUSE=/path/to/shared/cache
make packages/provider-core/check WHEELHOUSE=/path/to/shared/cache
```

`make checks/prepare WHEELHOUSE=... NETWORK_INTERFACE=...` combines checker
artifact acquisition and the offline checker build when both are needed.

Offline builds use captured inputs and disabled networking. Normal test commands
require prepared inputs; they do not download dependencies or rebuild the checker
automatically. Shared caches and prepared images survive operation cleanup.
If a command retains temporary inputs or receipts, inspect the reported paths
before removing them or retrying an uncertain operation.

## Build and run shared delivery

Build the runtime from a release artifact directory containing the issued wheel,
source distribution, complete installation lock, source record and hash manifest:

```sh
make delivery/image/build WHEELHOUSE=/path/to/shared/cache ARTIFACTS=/private/release/artifacts
```

The image build installs the issued wheel and closed runtime lock offline. It
contains shared delivery and API/acquisition proxies, with no scientific provider
source. It prints an immutable image ID; pass that ID explicitly to deployment
commands and provider deployments that use this runtime.

The operator-owned [binding definition](config/delivery.example.toml) is the
common authority for host delivery and provider configuration. Copy it to a
private, caller-owned mode 0600 file and set the project, installing UID, socket
GID, queue allowances, and each binding's API identity, credential/trust paths,
route and socket path. The host installs the complete definition; each provider
selects its named binding from the same definition. One through sixteen bindings
may share the service. Each per-provider limit must fit within its corresponding
host total; the totals bound the shared queue.

Each producer mounts only its binding's IPC volume read-only and adds the
configured socket GID. This socket grants local handover; the shared publisher's
API route and credentials belong to delivery. Scientific configuration and the
provider's own transport remain provider-owned.

Use one canonical private state root for each daemon-global Compose project:

```sh
mkdir -m 700 /private/delivery-state
make delivery/deployment/install DELIVERY_ROOT=/private/delivery-state DELIVERY_BINDINGS=/private/bindings.toml SHARED_RUNTIME_IMAGE=sha256:the-built-image-id
make delivery/deployment/up DELIVERY_ROOT=/private/delivery-state
make delivery/deployment/status DELIVERY_ROOT=/private/delivery-state
make delivery/deployment/logs DELIVERY_ROOT=/private/delivery-state
make delivery/deployment/down DELIVERY_ROOT=/private/delivery-state
```

Install captures the binding definition and selected credential, optional CA and
resolver files into a sealed recipe. Subsequent lifecycle commands use that
recipe. Local API routes attach the specified immutable Docker network ID;
mapped and external routes use proxies with host networking and an explicit
interface. The delivery daemon has no network and reaches the API through its
private proxy socket.

Ordinary `down` preserves all volumes and installed state. To adopt an existing
queue, inspect and drain it with its current owner where possible, then use that
owner's down/removal command to retire its project containers without removing
queue or IPC volumes. Preserve the project name, daemon UID/GID, API/provider
identities and socket leaves, then install the
complete binding definition here. Installation refuses existing project
containers; it does not retire another owner's service. Queue startup refuses a
definition that omits outstanding bindings. Adoption uses the existing queue;
it does not import results or rerun science. Per-root locking does not serialize
two roots selecting the same project.

For held results, stop delivery before inspecting its single-writer queue:

```sh
make delivery/deployment/held DELIVERY_ROOT=/private/delivery-state API_ORIGIN=https://api.example.invalid PROVIDER_REF=provider:your-provider
```

To remove one held payload, use `make delivery/deployment/discard` with the same
selectors plus its exact `EXECUTION_ATTEMPT_REF` and `BODY_SHA256` from the held
metadata. Discard removes only that matching held payload; it does not establish
API acceptance or resolve the remote Attempt.

## Work on this repository

Choose the maintained lane for the change:

| Command | Scope |
| --- | --- |
| `make tests/repository` | Repository conventions and detector fixtures |
| `make tests/repository/quality` | Code and operator conventions |
| `make tests/repository/provisioning` | Checker preparation and cleanup fixtures |
| `make tests/repository/runtime` | Checker container grants |
| `make tests/repository/command` | Launch preview without contacting Docker |
| `make tests/unit/provider-core WHEELHOUSE=...` | Installed-core unit tests |
| `make tests/contract/provider-core WHEELHOUSE=...` | Installed-package contracts |
| `make tests/integration/provider-core WHEELHOUSE=...` | Contained package integrations |
| `make tests/integration/provider-core/delivery WHEELHOUSE=...` | Delivery IPC and recovery |
| `make tests/deployment SHARED_RUNTIME_IMAGE=sha256:...` | Shared deployment freezer |

`make tests WHEELHOUSE=... SHARED_RUNTIME_IMAGE=sha256:...` runs repository,
unit, contract, integration and deployment lanes. Core tests install a private
wheel and use copied test files, without consumer checkouts or scientific
environments on the import path. Repository checks run offline with read-only
source, without credentials or an engine socket. These checks establish their
contained behavior; consumer integration, deployed-host cutover and scientific
correctness need evidence from the owning providers.

Ruff formatting is owned by the separate `ruffer` checkout. This repository
maintains its custom conventions and import-order checks.

## Build and publish a release

Release publication requires Git, an authenticated GitHub CLI (`gh`) and `jq` on
the host.

For a local artifact build, `make package/build WHEELHOUSE=... OUTPUT=...`
captures clean committed HEAD and writes five files: wheel, source distribution,
complete hash installation lock, source record and hash manifest. `OUTPUT` must
be an existing empty caller-owned mode 0700 directory. Use
`make tests/package WHEELHOUSE=... ARTIFACTS=...` on that directory to check the
exact files, rebuild offline from the source archive and test both installed
wheels. Keep release artifacts separate from the shared dependency cache.

For publication, author exactly one `notes/NNN_release_vX.Y.Z.md`, with one source
line per paragraph or list item. Commit it together with the matching package
version and source changes, then prepare the public inputs:

```sh
mkdir -m 700 /private/release-inputs
make publish/prepare TAG=vX.Y.Z WHEELHOUSE=/path/to/shared/cache OUTPUT=/private/release-inputs
```

Preparation builds and qualifies the five files under `artifacts/`, and retains
`release-build.log`, `release-test.log` and the authored `release-notes.md` beside
them. Review those exact public inputs, then publish from the unchanged clean
source:

```sh
make publish/release TAG=vX.Y.Z ARTIFACTS=/private/release-inputs
```

Publication checks the qualification hashes and authored note, requires the
canonical `numpde/nmr-provider-tools` GitHub destination, and refuses an existing
release. It pushes the tag, uploads a draft, downloads and compares every asset
and both logs, then makes the draft public. GitHub credentials stay in host
orchestration. Failed or uncertain publication retains its private receipts for
inspection; published assets are never replaced.

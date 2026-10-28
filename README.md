# NMR provider tools

Shared infrastructure for NMR API providers: the `provider_core` Python package,
durable terminal-result delivery, and tools to build and operate the delivery
service. Core supplies model-independent signed API requests, protocol handling,
clients and lifecycle/recovery helpers. Providers own scientific input
preparation, inference, model runners, offerings and model configuration.

Source is public at [numpde/nmr-provider-tools](https://github.com/numpde/nmr-provider-tools).
Packages are distributed through [GitHub Releases](https://github.com/numpde/nmr-provider-tools/releases),
including [v0.3.0](https://github.com/numpde/nmr-provider-tools/releases/tag/v0.3.0),
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
  -r /path/to/provider_core-0.3.0-install.lock
```

The [v0.3.0 release note](notes/011_release_v0.3.0.md) records the exact wheel
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
and live control bookkeeping; shared delivery owns durable pending result
storage. Provider integration should not add a second durable scientific-output
outbox or rerun inference to recover publication.

## Prepare a build host

The maintained commands require Linux/amd64, Bash, GNU Make, GNU coreutils and GNU
find, a non-root caller, and Docker with BuildKit at `unix:///var/run/docker.sock`.
Delivery commands require `jq`; `delivery/deployment/up` also requires Docker
Compose.
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
Package acquisition checks a complete wheel set offline first. With a partial
set, pip reuses compatible cached wheels and downloads missing files into private
temporary staging. Normal completion or failure cleans staging after the pip
processes exit; the reusable HTTP cache remains under `WHEELHOUSE/.pip-cache`.

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

To adopt this workflow in another repository, supply that repository's immutable
Python image and platform, complete version/hash locks for each independently
installed environment, and explicit URL/hash declarations for non-index files
(including sizes where declared). Each adopting repository declares its package
catalogs and direct artifact URLs with exact pins. This repository distributes
`provider_core` through GitHub Releases.
Pass the same flat `WHEELHOUSE` to each repository; `NETWORK_INTERFACE` selects
the interface for acquisition. Let pip select compatible files and verify locked
hashes for each lock separately, publish new verified files additively, then
capture the selected inputs for builds with networking disabled. Each repository
owns its Make commands, declarations and model-specific source preparation.

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
common authority for host delivery and provider configuration. Keep deployment
settings under `config/` and credentials under the Git-ignored `secrets/`
directory, or use equivalent operator-owned locations. Copy the example to your
delivery configuration and set the project, runtime UID matching the caller,
socket GID, queue allowances, and each binding's API identity, credential/trust paths, route and
socket path. Use canonical absolute file paths for the definition and its file
inputs. The host reads the complete definition; each provider selects its named
binding from the same definition. One through sixteen bindings may share the
service. Each host total must cover the sum of all bindings' per-provider
allowances; the totals bound the shared queue.

Each producer mounts only its binding's IPC volume read-only and adds the
configured socket GID. This socket grants local handover; the shared publisher's
API route and credentials belong to delivery. Scientific configuration and the
provider's own transport remain provider-owned.

Start from the binding definition and immutable runtime image. For the example
below, the definition selects `project = "nmr-api-delivery"`:

```sh
make delivery/deployment/up DELIVERY_BINDINGS=/absolute/path/config/delivery.toml SHARED_RUNTIME_IMAGE=sha256:the-built-image-id
make delivery/deployment/status PROJECT=nmr-api-delivery
make delivery/deployment/logs PROJECT=nmr-api-delivery
make delivery/deployment/down PROJECT=nmr-api-delivery
```

`up` renders a temporary Compose definition and mounts only the exact operator
configuration, credential, optional CA and resolver files read-only. Keep those
original files available and readable by the configured UID for container
restarts. The daemon checks the binding definition against the bytes selected by
`up`; to change it, run `down`, edit the inputs and run `up` again. The derived
daemon configuration lives in container `/run` tmpfs. Docker volumes hold the
SQLite queue, producer IPC sockets and private API-proxy socket state.

Local API routes attach the specified immutable Docker network ID; mapped and
external routes use proxies with host networking and an explicit interface. The
delivery daemon has no network and reaches the API through its private proxy
socket. `status`, `logs` and `down` select existing containers by `PROJECT` and
check their delivery ownership; they do not require a saved host recipe.

Ordinary `down` stops and removes project containers while preserving all
volumes. `up` requires previous project containers to have been removed, including
stopped containers. To adopt an existing queue, inspect and drain it with its
current owner where possible, then use that owner's down/removal command to
retire its project containers without removing
queue or IPC volumes. Preserve the project name, daemon UID/GID, API/provider
identities and socket leaves, then run `up` with the complete binding definition.
This launcher does not retire another owner's service. Queue startup refuses a
definition that omits outstanding bindings. Adoption uses the existing queue;
it does not import results or rerun science.

For held results, use `down` to remove delivery containers before opening its
single-writer queue. Supply the binding definition, runtime image and exact
API/provider selectors:

```sh
make delivery/deployment/held DELIVERY_BINDINGS=/absolute/path/config/delivery.toml SHARED_RUNTIME_IMAGE=sha256:the-built-image-id API_ORIGIN=https://api.example.invalid PROVIDER_REF=provider:your-provider
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
| `make tests/deployment SHARED_RUNTIME_IMAGE=sha256:...` | Delivery rendering and proxy boundaries |

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

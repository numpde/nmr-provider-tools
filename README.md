# NMR provider tools

The code here helps NMR providers talk to the API, deliver results and deploy. Each provider brings its own scientific code.

Get `provider_core` and its lock file from [GitHub Releases](https://github.com/numpde/nmr-provider-tools/releases). The [package docs](packages/provider-core/README.md) explain how to use it.

Shared delivery stores results and retries sending them to the API. Start with the [example config](config/delivery.toml.example) and save the host's copy under `config/deployments/<name>/settings.toml`. Named deployment settings are local and ignored by Git. Providers use their own repository's deployment settings to identify the shared socket grants; they do not read this file. Keep the settings directory searchable by its group and the file readable by that group (for example, modes `0710` and `0640`). When replacing the file, preserve its group and group-read permission so live reload continues to work.

Prepare the checked-in delivery release in the shared wheelhouse, then start the
named deployment:

```sh
make delivery/prepare DOCKER_CONTEXT=science WHEELHOUSE=/path/to/wheelhouse NETWORK_INTERFACE=system
make delivery/deployment/up DOCKER_CONTEXT=science DEPLOYMENT=localhost
```

On a new host, first run `make checks/base-image/download DOCKER_CONTEXT=science` to acquire the pinned
container base through Docker. Preparation downloads missing pinned wheels,
verifies them, and builds offline.
`system` uses the host's normal network routing; an interface name binds
acquisition to that interface. When selecting a new delivery release, update
`requirements/delivery-release.json` and `requirements/delivery-runtime.lock`
together. Verify the issued `provider_core-<version>-install.lock` against the
pin's `install_lock_sha256`, then copy the bytes before its final `provider-core`
requirement into the runtime lock. Download the issued lock directly over HTTPS
from GitHub Releases if you need a copy. Prepare the new release to build its
image. Starting the outbox retains its queue and leaves an
existing proxy running. `make delivery/deployment/proxy/restart DOCKER_CONTEXT=science DEPLOYMENT=localhost`
updates the proxy separately. Status, logs, and down use `PROJECT` from the
settings file so they remain available for recovery without a prepared image.

The acquisition owner also accepts an upstream `http://`, `socks5://` or
`socks5h://` URL through `acquisition_set_route`. The shared gateway owns that
connection; workers keep their isolated loopback HTTP proxy and cannot fall
back to direct egress. `socks5h` delegates destination DNS to the upstream proxy.
Proxy URLs require an explicit port and do not accept embedded credentials.
The bootstrap bundle includes the hash-pinned `python-socks` transport, so
consumer launchers need no installed SDK or SOCKS implementation on the host.

Acquisition captures DNS configuration once per operation. A named interface
uses its configured NetworkManager DNS first, then systemd-resolved link DNS;
if neither supplies usable IPv4 resolvers, it uses the host's configured global
resolvers. The `system` route uses global configuration directly. DNS queries
and destination connections remain bound to the selected route, with no
hard-coded public fallback. Configuration changes apply to the next operation.

The shared source bundle includes `packages/acquisition/acquisition_tool/dns.py` for host-owned build
launchers. Invoke it with `--interface <name> --output <private-scratch>/resolv.conf`,
then give that file to the proxy with `--resolver-file`. Keep it until the proxy
has retired. Containers receive the captured file, not host manager sockets or
tools. See [NetworkManager's device interface](https://networkmanager.dev/docs/api/latest/nmcli.html)
and [systemd-resolved's link DNS](https://www.freedesktop.org/software/systemd/man/latest/resolvectl.html).

Outbox byte limits count terminal request bodies, not all disk use. Allow space
for each retained handoff's encoded credential and CA (up to 16 KiB and 1 MiB
before encoding), plus SQLite database and journal overhead. Item limits bound
the number of retained handoffs. A healthy process alone does not prove every
provider is publishing; use the operator status and logs to inspect progress.
`make delivery/deployment/status DOCKER_CONTEXT=... PROJECT=...` shows a private, fresh aggregate
observation of pending and held work, oldest pending age, paused publishers,
and body and publication material sizes. Queue counts are sampled at most once
every five seconds; the output includes their age. Inspect available space on
the volume's host filesystem separately. An unavailable observation means
unknown, not an empty queue, and queue status does not prove API acceptance.

The shared proxy delays new connections as its forwarding slots fill: no
intentional delay through 50% occupancy, rising linearly to seven seconds at
100%. Change `api_proxy.max_admission_delay_seconds` in the same settings file
to adjust the maximum for new connections, from zero (no ramp) to 30 seconds.
Waiting is bounded and consumes the caller's existing timeout, so sustained
overload can still fail a connection.
`api_proxy.admission_order` selects `ready` (the default, start the oldest
connection whose delay has elapsed) or `fifo` (preserve arrival order, even
when an earlier connection's delay holds up others). Both settings reload live.


## Updating checked-in bootstrap source

`requirements/provider-tools-source.json` selects the acquisition source release
and its archive SHA-256. After reviewing that pin, update the checked-in snapshot:

```sh
make provider-tools/update-checked-in-source-from-pinned-release
```

This maintenance command downloads the public archive directly over HTTPS using
Python's standard library. The reviewed maintenance package verifies the target
as data and checks the selected hash and archive before replacing clean source.
The selected pin is captured before downloading; editing the pin during a transfer does not
retarget that update. Bootstrap updating requires neither Git nor `gh`.


## Preparing and publishing a provider-core release

Release publication builds and qualifies new artifacts, then publishes them to
GitHub using Git and `gh`. Its approval, draft verification and publication
workflow is separate from downloading an existing bootstrap source release.

`publish/prepare` writes a reviewed, versioned release bundle into an explicit
`OUTPUT` directory; `WHEELHOUSE` is the separate shared dependency cache.
Create a new, empty, caller-owned mode-`0700` directory before invoking:

```sh
mkdir -m 700 -- release-artifacts/v0.6.8
make publish/prepare TAG=v0.6.8 DOCKER_CONTEXT=science \
  WHEELHOUSE=/actual/shared/cache \
  OUTPUT="$PWD/release-artifacts/v0.6.8"
```

The output contains `artifacts/` (eight exact release inputs),
`release-build.log`, `release-test.log`, and `release-notes.md`. Inspect
them before publishing with
`make publish/release TAG=v0.6.8 ARTIFACTS="$PWD/release-artifacts/v0.6.8"`.
`ARTIFACTS` must point to the whole prepared directory, not to its
`artifacts/` child. Preparation never publishes; neither phase overwrites
previous release evidence.

Installed SDK test failures end with a **PROVIDER-CORE FAILURE SUMMARY**:
test and subtest names, source locations and short causes, with full unittest
tracebacks preserved above it. An unowned asyncio ERROR (including SSL handshake
resets and task errors) is a failing test and appears in the same index, even
when emitted during loop shutdown. Expected injected asyncio errors must be
explicitly captured and asserted within their test (for example with
`assertLogs("asyncio", level="ERROR")`); no exception type is globally exempt. A failed `publish/prepare` prints the summary
immediately and leaves `release-failures.txt` and the complete
`release-test.log` or `release-build.log` in the selected OUTPUT directory.
Failed artifacts are not eligible for publication. See `make help` for all
targets and arguments.

Run `make help` to see the available commands. Builds, tests and deployments use Docker on Linux/amd64.

Run the reproducible failure-sequence corpus against the installed package with
`make tests/fuzz/provider-core DOCKER_CONTEXT=science WHEELHOUSE=/path/to/wheelhouse`.
It covers local TLS stalls, cooperative and blocked cleanup, interpreter retries
and handoffs, and durable outbox replay/restart histories. Failures identify their
seed and scenario. These cases also run in the ordinary unit/integration suites;
they use synthetic inputs and make no external model calls. See the
[resilience plan](notes/058_resilience_fault_sequence_tests.txt) for scope and limits.

Select a local Docker context for builds, tests and deployments with
`DOCKER_CONTEXT=<name>` on the Make command line, or choose one interactively.
There is no default. Contexts must use a Unix socket on the machine running
the command. Run operations on the target machine so bind mounts and network
interfaces refer to that host.

## Authored execution boundaries

`host/tools/` contains the Bash orchestrators and the Python helpers they invoke
on the operator's host. `packages/acquisition/acquisition_tool/` owns the authored acquisition
owner and resolver/identity sources. Host Python does not depend on the
container-only `tools` package.

`operations/docker/` owns local Docker context selection, client setup, image
lifecycle and engine cache maintenance. Its container runner handles cleanup
for SDK builds, delivery deployments and repository checks.

`operations/checker/` prepares the checker image from its Dockerfile and
declared dependencies. The repository test launcher and the live checker
provisioning check live alongside their suites in `tests/repository/`.

`operations/delivery/` builds the shared delivery image and manages its
deployment. Its `image/` directory contains the Dockerfile, the deployment
entry point installed in the image and the runtime check used during its build.

`packages/wheelhouse-tool/` owns the wheelhouse CLI, its image and bundle
builders, and its operator launchers. Its Python import name is `wheelhouse_tool`.

For a deployment lock, run the released bundle inside the target Python image:

```sh
python wheelhouse-tool.pyz locate --lock release-install.lock --index-url https://pypi.org/simple > pylock.target.toml
python wheelhouse-tool.pyz requirements --lock pylock.target.toml --source-lock release-install.lock > requirements.txt
```

`locate` reads the package index metadata under the release lock's existing
hashes; `requirements` performs the target selection offline. The output pins
one exact wheel URL and hash per package, so another Python version or platform
requires regeneration. Keep the portable release lock as the authority, rather
than copying its cross-platform hash inventory into every deployment. For the
SDK wheel, supply `locate --source <release-wheel-URL>#sha256=<release-wheel-hash>`;
the selector checks it against the SDK pin in the release lock. For wheels hosted
in a known artifact directory, such as PyTorch, supply `--pool <cache-dir>` and
`--source-base torch=https://download.pytorch.org/whl/cpu`. The locator projects
only cached wheels that match the pinned version, hashes and target tags; it
constructs their URLs from that directory, without falling back to the index.

`packages/provider-core/sdk_build/` acquires SDK dependencies, builds distributions
and runs tests against the installed SDK. `operations/release/` qualifies the
resulting release artifacts and publishes them to GitHub.

`packages/type-requirements/src/type_requirements.py` owns the standard-library
type checks shared by the checker, installer, and SDK. The maintained build
targets include that source in each artifact, and SDK callers import it from
`provider_core.type_requirements`.

`tools/` contains shared Python modules for container operations.
`packages/provider-core/src/provider_core/bound_http_proxy.py` owns the acquisition
proxy source; the acquisition builder includes it from the candidate SDK wheel.
The installed `provider_core` package contains both portable scientific contracts and guarded operational
modules. The `bootstrap/acquisition/` tree is a pinned release snapshot; its
internal `tools/` and `acquisition_tool/` paths are deliberately unchanged.

Public Make targets remain the supported interface. The separation applies to
source ownership and execution policy, not to the names of published bundle
members or the operator CLI.

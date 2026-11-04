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
acquisition to that interface. Updating the checked-in release pin and preparing
it again selects a new image. Starting the outbox retains its queue and leaves an
existing proxy running. `make delivery/deployment/proxy/restart DOCKER_CONTEXT=science DEPLOYMENT=localhost`
updates the proxy separately. Status, logs, and down use `PROJECT` from the
settings file so they remain available for recovery without a prepared image.

Acquisition captures DNS configuration once per operation. A named interface
uses its configured NetworkManager DNS first, then systemd-resolved link DNS;
if neither supplies usable IPv4 resolvers, it uses the host's configured global
resolvers. The `system` route uses global configuration directly. DNS queries
and destination connections remain bound to the selected route, with no
hard-coded public fallback. Configuration changes apply to the next operation.

The shared source bundle includes `acquisition_tool/dns.py` for host-owned build
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

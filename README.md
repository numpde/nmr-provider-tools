# NMR provider tools

The code here helps NMR providers talk to the API, deliver results and deploy. Each provider brings its own scientific code.

Get `provider_core` and its lock file from [GitHub Releases](https://github.com/numpde/nmr-provider-tools/releases). The [package docs](packages/provider-core/README.md) explain how to use it.

Shared delivery stores results and retries sending them to the API. Start with the [example config](config/delivery.toml.example) and save the host's copy under `config/deployments/<name>/settings.toml`. Named deployment settings are local and ignored by Git. Providers use their own repository's deployment settings to identify the shared socket grants; they do not read this file.

Prepare the checked-in delivery release in the shared wheelhouse, then start the
named deployment:

```sh
make delivery/prepare WHEELHOUSE=/path/to/wheelhouse NETWORK_INTERFACE=system
make delivery/deployment/up DEPLOYMENT=localhost
```

On a new host, first run `make checks/base-image/download` to acquire the pinned
container base through Docker. Preparation downloads missing pinned wheels,
verifies them, and builds offline.
`system` uses the host's normal network routing; an interface name binds
acquisition to that interface. Updating the checked-in release pin and preparing
it again selects a new image. Starting the outbox retains its queue and leaves an
existing proxy running. `make delivery/deployment/proxy/restart DEPLOYMENT=localhost`
updates the proxy separately. Status, logs, and down use `PROJECT` from the
settings file so they remain available for recovery without a prepared image.

Outbox byte limits count terminal request bodies, not all disk use. Allow space
for each retained handoff's encoded credential and CA (up to 16 KiB and 1 MiB
before encoding), plus SQLite database and journal overhead. Item limits bound
the number of retained handoffs. A healthy process alone does not prove every
provider is publishing; use the operator status and logs to inspect progress.
`make delivery/deployment/status PROJECT=...` shows a private, fresh aggregate
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

# NMR provider tools

The code here helps NMR providers talk to the API, deliver results and deploy. Each provider brings its own scientific code.

Get `provider_core` and its lock file from [GitHub Releases](https://github.com/numpde/nmr-provider-tools/releases). The [package docs](packages/provider-core/README.md) explain how to use it.

Shared delivery stores results and retries sending them to the API. Start with the [example config](config/delivery.example.toml) and use the same file for the service and its providers.

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

Run `make help` to see the available commands. Builds, tests and deployments use Docker on Linux/amd64.

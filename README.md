# NMR provider tools

The code here helps NMR providers talk to the API, deliver results and deploy. Each provider brings its own scientific code.

Get `provider_core` and its lock file from [GitHub Releases](https://github.com/numpde/nmr-provider-tools/releases). The [package docs](packages/provider-core/README.md) explain how to use it.

Shared delivery stores results and retries sending them to the API. Start with the [example config](config/delivery.example.toml) and use the same file for the service and its providers.

Run `make help` to see the available commands. Builds, tests and deployments use Docker on Linux/amd64.

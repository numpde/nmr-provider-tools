# provider-core 0.2.0

This first shared release moves model-independent provider operations and durable terminal-result delivery into `nmr-provider-tools`. Providers keep their scientific preparation, inference, model runners, offerings and model-specific configuration.

The package owns signed API requests, protocol handling, the delivery client and reusable lifecycle/recovery helpers. `DeliveryClient` can hand over a result without importing the optional execution controller or recovery modules. A matching durable enqueue acknowledgement transfers the exact result to shared delivery; it is not an API acceptance receipt.

The release requires Python 3.12 or newer. It includes the wheel, source distribution, complete hash installation lock, source record and artifact hash manifest. Dependencies were reviewed before installation; builds, installation and qualification use captured wheels with networking disabled. This package is released through GitHub Releases.

New queue allowances support at most 256 items per provider binding so complete pending inventory fits the supported IPC envelope. Valid larger legacy queues continue publishing and retain every row. Oversized complete inventory returns a bounded refusal; individual Attempt status remains available through a direct lookup.

Published from committed source `a30ec2ff8f5cf60a464408309781b7996baae3ad`. The exact wheel passed 476 unit tests, 15 installed-package contracts and 147 integration tests. The source distribution rebuilt offline, and its installed wheel passed another 15 contracts. Downloaded draft assets and both qualification logs matched the qualified bytes before publication. These are package and contained component proofs; consumer adoption and deployed-host cutover remain separate.

The release includes the [build log](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.0/release-build.log), [test log](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.0/release-test.log), [source record](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.0/provider_core-0.2.0-release.json), [artifact hashes](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.0/provider_core-0.2.0-sha256.json) and [complete install lock](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.0/provider_core-0.2.0-install.lock).

```text
provider-core @ https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.0/provider_core-0.2.0-py3-none-any.whl#sha256=248bcf548a25edb5d3b349e024d2ace568a5f9d2d01474d7a14d2c1d0394f6da
```

Acquire that wheel and its locked third-party wheels, then install the complete lock in a fresh environment with `pip install --no-index --no-deps --only-binary=:all: --find-links /path/to/cache --require-hashes -r provider_core-0.2.0-install.lock`.

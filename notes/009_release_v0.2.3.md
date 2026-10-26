# provider-core 0.2.3

This patch distinguishes the SDK's own pre-connection startup deadline from socket and greeting timeouts, allowing providers to preserve readiness-unavailable behavior when no control connection was made.

`RunnerStartupUnavailable` extends `TimeoutError` and is emitted only when the SDK's pre-attempt deadline expires: before the first attempt or after known missing/refused control-endpoint attempts. A successful connection leaves that loop immediately. Arbitrary operating-system and greeting timeouts retain their original identity and uncertainty. Providers inspect the outcome after joining the original setup operation and retain ownership of availability and recovery policy; the SDK adds no automatic retry or retirement receipt.

The change closes a real Magnet receiving failure: a missing control endpoint exhausted its deadline, then generic startup cleanup tried to read retirement from a never-connected socket and masked the readiness-unavailable result. The shared regression witnesses exercise real missing/refused endpoints and preserve foreign socket and partial-greeting timeout identity. Existing finite and owner-session lifetimes, scientific readiness and request deadlines remain unchanged.

The exact wheel passed 502 unit tests, 15 installed-package contracts and 158 contained integration tests. The source distribution rebuilt offline and its installed wheel passed another 15 contracts. These are package and contained component proofs; consumers own their deployed adoption and live evidence.

Python 3.12 or newer is required. Runtime and build dependency versions are unchanged from 0.2.2. Distribution remains through GitHub Releases with a wheel, source distribution, complete hash installation lock, source record and artifact hash manifest. Published 0.2.2 assets remain unchanged.

The release includes the [build log](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.3/release-build.log), [test log](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.3/release-test.log), [source record](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.3/provider_core-0.2.3-release.json), [artifact hashes](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.3/provider_core-0.2.3-sha256.json) and [complete install lock](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.3/provider_core-0.2.3-install.lock).

```text
provider-core @ https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.3/provider_core-0.2.3-py3-none-any.whl#sha256=2b5fe22d9bab969b83445e9ac3d839ad9c980c226ad53893e70776f3bc75e376
```

Acquire that wheel and its locked third-party wheels, then install the complete lock in a fresh environment with `pip install --no-index --no-deps --only-binary=:all: --find-links /path/to/cache --require-hashes -r provider_core-0.2.3-install.lock`.

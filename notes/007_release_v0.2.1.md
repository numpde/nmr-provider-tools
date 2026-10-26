# provider-core 0.2.1

This patch preserves provider API recovery through rate limits and server failures, and moves offering-field admission and CA parser diagnostics to their shared owners.

`ProviderAPI.can_retry_read` permits paced fresh GETs after settled HTTP 429 and 500, alongside the existing gateway and temporary-I/O cases. `can_refresh_hello` permits a fresh complete current announcement after those responses; it never replays an old snapshot or confirms an earlier send. Callers retain their stop checks, pacing, observation deadlines and request-ID evidence for operator diagnosis. Permission does not promise that the service will recover.

`can_retry_job_discovery` additionally permits the exact Jobs GET to wait through complete correlated authentication and authorization refusals. This discovery policy does not authorize scientific execution or apply those access refusals to other operations.

A complete correlated `request_body_timeout` HTTP 408 now admits fresh signing and reports that this send did not run the operation. Any earlier unanswered send remains unresolved. Existing operation-specific rules still own retained START keys and exact bodies, progress recovery and terminal replay; fresh-signing permission alone grants no mutation or capability-issuance retry.

`AnalysisOffering` validates its kind and description at construction, allowing a provider to attach its model identity to an error. Length refusals supply the observed character count and limit without description contents. Aggregate Hello encoding retains collection, uniqueness and byte-budget admission. CA loading distinguishes non-ASCII PEM text from OpenSSL certificate-loading failures and retains protected native causes; the selected file reader still owns file and byte-limit admission.

The exact wheel passed 485 unit tests, 15 installed-package contracts and 147 contained integration tests. The source distribution rebuilt offline and its installed wheel passed another 15 contracts. Qualification also verifies that terminal START state prevents science independently of the received replay flag. These are package and contained component proofs; each consumer owns its deployed adoption and live evidence.

Python 3.12 or newer is required. Runtime and build dependency versions are unchanged from 0.2.0. Distribution remains through GitHub Releases with a wheel, source distribution, complete hash installation lock, source record and artifact hash manifest.

The release includes the [build log](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.1/release-build.log), [test log](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.1/release-test.log), [source record](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.1/provider_core-0.2.1-release.json), [artifact hashes](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.1/provider_core-0.2.1-sha256.json) and [complete install lock](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.1/provider_core-0.2.1-install.lock).

```text
provider-core @ https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.1/provider_core-0.2.1-py3-none-any.whl#sha256=3c15c53aefc4583e86a40556a93c30cb99ee9492fab30fd761dbc31da82aaf9f
```

Acquire that wheel and its locked third-party wheels, then install the complete lock in a fresh environment with `pip install --no-index --no-deps --only-binary=:all: --find-links /path/to/cache --require-hashes -r provider_core-0.2.1-install.lock`.

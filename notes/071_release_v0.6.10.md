# provider-core 0.6.10

This release updates the shared acquisition bundle to linux-idmap 0.3.2 and makes the canonical acquisition payload conform to the consumer source policy. Identity bootstrap remains isolated from ambient Python imports and now separates its small isolated entrypoint from implementation imports. The unused proxy entrypoint is removed; gateway and relay continue using the provider-core proxy module.

Acquisition cleanup admits only matching container receipts and create output, including cancellation during creation. Removal is attempted once; a retired removal client is followed by read-only absence confirmation, preserving its original failure. An uncertain removal retains evidence and blocks dependent cleanup. Common failure regressions live with the SDK acquisition owner.

Readiness failures distinguish stopped containers, failed or malformed observations, and exhausted checks. Raw engine causes and bounded container logs remain private in the existing operation directory after container retirement. Secondary log failures do not replace the primary failure. Cancellation during diagnostic reporting reaches the restored caller signal disposition; an ordinary signal-number exit status is not treated as cancellation.

SDK bootstrap replacement verifies the existing snapshot against its recorded release before staging a new snapshot and issuer. Modified files, executable bits, unexpected files and unrecorded directories are refused. Members owned only by the previous release can be removed through the verified transaction, with rollback on issuer publication failure.

Exact release artifacts are qualified by the maintained preparation lane. Provider adoption, scientific model inference and production deployment are separate operations.

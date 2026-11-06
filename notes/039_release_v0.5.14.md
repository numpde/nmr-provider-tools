# provider-core 0.5.14

The wheelhouse tool retries transient metadata and artifact network failures within the existing acquisition worker. Retries are bounded, preserve locked hashes and no-overwrite publication, and report a clear error when recovery is exhausted or the failure is not retryable.

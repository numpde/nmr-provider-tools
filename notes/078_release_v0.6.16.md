# provider-core 0.6.16

Acquisition callers can supply a captured Compose overlay for package commands and mounts. The SDK supplies the common interface-bound gateway, isolated relay, and online/offline worker topology. The existing acquisition scope remains the owner of the selected engine, mapped identity, cancellation, result collection, and cleanup. The four-argument Compose worker returns its package result only after retirement is confirmed. Existing argv workers remain available for their current callers.

The acquisition source archive includes the Compose controller and topology. Package builds capture those assets from the same committed source as the SDK. Online Compose workers override inherited proxy and bypass settings consistently with the existing worker path.

Successful HTTP exchanges complete TLS shutdown before closing the underlying connection. Interrupted exchanges abort unfinished responses and retain their original failures, including when the HTTP library starts cleanup before returning an error. Regression tests distinguish a completed HTTP response from a clean TLS shutdown through both direct and Unix-proxy connections.

Proxy logs retain original exception messages, tracebacks, cause chains, and recorded DNS failures. Relay exceptions gain direction, operation, and attempted-byte context without replacement exceptions or guesses about HTTP acceptance. A custom log formatter scrubs recognizable credentials from final output while leaving the original exceptions and log arguments intact. It does not identify every private value; proxy code does not deliberately dump request headers, payloads, or frame locals.

The acquisition archive retains linux-idmap 0.3.3 and the existing verified installer inputs. This release does not deploy providers or migrate every acquisition caller to Compose. Rootful, bypass, and exhaustive failure-matrix qualification remain deferred.

# provider-core 0.6.18

Completed, framed HTTP responses remain usable when the TLS peer disconnects without sending close_notify, provided the original raw socket close completes. The original TLS shutdown diagnostic is logged through the logging boundary. Truncated responses, malformed framing, decoder failures, byte ceilings and real raw-close failures still reject the exchange.

AnyIO's internal forceful close now reports completion to the SDK's original stream owner. If forceful TCP closure is interrupted after closing its descriptor, cleanup retries raw close rather than re-entering the failed TLS shutdown. Cancellation and deadlines remain failures, while confirmed raw closure no longer leaves a fictitious cleanup obligation behind. Pending physical close still holds its original owner and refuses replacement work.

The regression matrix adds 41 deterministic scenarios and five bounded shrinking Hypothesis properties, including real TLS sockets and CONNECT tunnels. Persistent and transient EOF-shaped raw-close faults remain errors even when a subsequent close succeeds. Existing clean TLS shutdown and proxy controls remain in the qualification suite.

This correction belongs to the HTTP client transport. Updating a shared proxy does not replace the SDK already installed in a provider process; affected providers must also adopt this release.

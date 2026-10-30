# provider-core 0.5.3

This release adds a push-only shared delivery intake. Providers hand off the exact
terminal result together with their original signing credential, API settings and
optional CA. Delivery commits those inputs together and signs each publication
attempt independently, including after a provider or delivery restart. A local
acknowledgement proves durable enqueue, not API acceptance. Exact replays are
acknowledged; a different result or publication material for the same outstanding
Attempt is refused. The intake exposes no queue-read operation.

The shared API CONNECT proxy has independently reloadable capacity, destination
policy and logging. Its group-access socket can be borrowed by providers and
delivery while its owner and readiness remain separate. Delivery reloads queue
limits, retry policy and logging from the same host-owned settings file. Lowering
capacity does not delete accepted work. Existing legacy rows without established
original publication inputs are retained for operator reconciliation.

This release provides the shared SDK and service mechanisms. A release or passing
fixture does not establish provider adoption or a running joint deployment; those
are verified in their owning deployment environments.

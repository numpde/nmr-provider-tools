# Provider-core 0.6.1

Fix repeated acquisition with the shared bootstrap. The identity helper now
passes `-B` explicitly, so isolated Python imports leave the verified source
unchanged. Version 0.6.0 could write bytecode into a writable checkout and
cause the next bootstrap verification to refuse it; consumers should use this
release instead.

Identity mapping now probes the pinned bootstrap image once per scope and
reuses the result under the same selected engine and default user namespace
policy. Workers no longer need a Python interpreter for identity probing.
The owner retains control of user namespace selection; there is no additional
provider compatibility setting.

The maintained real-engine check runs two complete offline/online/offline
sessions from the same source snapshot and verifies its inventory after each.
Scientific API and shared proxy/outbox behavior remain unchanged.

# provider-core 0.6.6

Interpreter requests that exhausted an inherited deadline could report resource retirement as unconfirmed before promptly cancellable I/O had a chance to close. The original exchange now gets one separately bounded cleanup allowance. Repeated retirement calls share that allowance; blocked cleanup stays owned and cannot authorize another request. Request deadlines still prohibit new work and late successful responses. Existing API, upload and outbox calls without an inherited deadline retain their bounded re-observation behavior.

`AttemptExecutor.delivery_accepted` exposes the existing verified relay acknowledgement independently of cleanup. Providers can clear delivery pressure for unrelated Jobs while retaining the original controller and its unfinished cleanup. This does not prove API publication, release the full claim, or permit scientific replay.

The installed-package test lanes now include 88 reproducible generated scenarios: local TLS stalls, cooperative and held cleanup, interpreter retry/repair/handoff, and 1,024 outbox intake/replay/conflict/hold/publication/restart transitions. `make tests/fuzz/provider-core` runs this corpus separately. Tests use synthetic inputs and local peers; they do not establish live model quality or scientific inference. The broader repository quality debt is recorded in note 058.

Providers need the new SDK for interpreter cleanup and the acceptance predicate. This release requires no shared API proxy or outbox replacement.

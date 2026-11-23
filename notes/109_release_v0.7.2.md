# provider-core 0.7.2

`JobService` runs a serial API job loop around a persistent scientific host.
Providers supply their scientific `AnalysisSession`, API and shared-delivery
factories, offering metadata, and explicit polling and recovery intervals.
The service delegates START, phase observation, result handoff, and Attempt
settlement to the existing SDK owners.

`ScientificCall` accepts a `require_execution` callback. API sessions pass their
Attempt's permission check so cancellation during retry backoff prevents the
next calculation. A refused check propagates without another retry.

Service shutdown retains the original Attempt and selected result through
shared-delivery backpressure. It does not turn an unknown scientific outcome into
a public failure. Persistent delivery unavailability can keep shutdown pending.
Worker exit stops the service, and shutdown attempts an unavailable announcement
even when the first available announcement's reply was lost. API unavailability
can prevent confirmed withdrawal; the original failure remains visible. Jobs
with an existing provider Attempt are excluded from discovery; resuming an
in-progress Job after a process restart is outside this service.

The released `wheelhouse-tool` now supports `requirements --lock` to emit a
pip lock for the artifacts selected by the running interpreter. Use `locate`
against the portable release lock first, then `requirements --source-lock` to
retain its hash authority while emitting only one compatible wheel per package.
Deployment repositories no longer need the cross-platform hash inventory.

Both the native and API install locks include the SDK wheel and its dependency
closure. The API provider needs the API lock; scientific workers can use the
smaller native lock. Existing 0.7.1 scientific process messages remain compatible.

Attached qualification logs identify the tested release artifacts. Repository
quality findings remain recorded in `notes/092_repo_checks_status.html`; release
qualification does not establish a deployed ChefNMR2 API job result.

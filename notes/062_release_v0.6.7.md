# provider-core 0.6.7

A cancelled Job or expired Attempt confirmed by the NMR Jobs API can settle its original pre-execution resources and release the claim once. The SDK exposes the checked disposition to provider loops without treating an operator's local stop, an uncertain START or entered science as a server revocation. Unresolved work and unacknowledged reports remain owned.

Prose interpretation now sends each outbound model HTTP request in its own bounded Python process. The provider retains conversation, scientific preparation and Attempt ownership. Original request deadlines, framed responses, TLS verification, cooperative close and TERM/KILL escalation have independently checked cleanup boundaries. An unconfirmed child exit does not free capacity or authorize another scientific request. The process carrier requires the Linux/POSIX environment used by provider containers; unsupported opaque TLS customizations are explicitly refused.

`ProseService` carries one configured language-model service's backoff and valid `Retry-After` between Jobs. A subsequent interpretation cannot bypass that service's outstanding delay, while another configured service may remain available. Providers should construct these service bindings once at startup and keep prose-dependent Jobs unstarted while all alternatives are delayed; the SDK does not own the provider's Job-discovery loop.

Result publication now reports elapsed wait for the original retained report. One `delivery_stalled` warning also covers a matching NMR Jobs API receipt followed by an uncompleted local queue update. These observations do not cancel storage, replay an accepted report, or release an unfinished Attempt. Protected diagnostics retain the age of the original execution failure.

The local B4 SDK and consumer fault tests use HTTPS peers, bounded outages, counted scientific operations and exact publication receipts. They do not qualify the native four-provider deployment. In particular, reconstructed SECS and NMRNet branches require new installed-artifact testing, and the previous environment blocked three Unix-domain-socket SDK tests. Complete release artifact qualification and positive native/container deployment checks remain release gates; see notes 061 and 060.

This version does not implement the later shared provider-service extraction or recovery of selected results lost when a provider dies before durable outbox handoff. No NMR Jobs API schema change is required.

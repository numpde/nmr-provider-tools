# Provider core

Model-independent signed NMR API operations, protocol admission, durable result
delivery and reusable lifecycle helpers. Providers own scientific preparation,
inference, offerings, model configuration and their execution policy.

The delivery client submits exact complete/fail request bytes to a configured
provider-specific Unix socket. Its acknowledgement proves durable local enqueue;
the shared delivery service then owns API publication and retries. It does not
prove that the API has accepted the result. Lost acknowledgements and transport
retirement remain explicit outcomes; a missing queue row does not prove that a
previous request was never delivered.

The host and provider deployment select the same operator-owned neutral binding
definition for API identity, credential and CA paths, API routing and socket
grant. Scientific configuration and producer recovery state remain provider-owned.

The native supervisor requires one explicit lifetime: `--operation-seconds N`
caps one child session, while `--owner-session-lifetime` allows a loaded child
to serve repeated scientific requests. Child exit, control-owner stop or
disconnect, and supervisor shutdown still trigger bounded retirement and scratch
cleanup. Startup, TERM, KILL and cleanup budgets remain mandatory. The provider
owns scientific readiness, request deadlines and cleanup between requests; the
control greeting does not prove that a model is ready.

`RunnerSession.request_stop()` retains a stop requested before connection and
wakes endpoint retries. Once connected, the original greeting reader retains
its startup deadline; join that setup operation before retirement or close.
Use one absolute provider startup deadline across control setup and scientific
readiness. A failed greeting prevents another control read, and closing the
session does not prove child or scratch retirement.

After setup joins, `control_connected` records whether control connect returned
successfully, including after close. It proves neither greeting admission nor
cleanup. Only the SDK's `RunnerStartupStopped` with no successful connection
identifies its pre-connection stop path; arbitrary OS failures, including an
ordinary `InterruptedError`, retain their original identity and uncertainty.

`ToolConversation` keeps one bounded transcript and permits endpoint fallback
only after a carrier failure. Providers own endpoint selection and scientific
tool dispatch. An explicit `text_correction=TextCorrection(user_content=...)`
grant sends each safe rejection as plain tool text, followed by the exact
reviewed user instruction. It also permits absent/null tool-call replies to
consume a repair turn without inventing a tool result. Canonical admitted
arguments survive caller rejection; invalid or unparsed arguments remain
erased. Omitting the grant retains JSON rejection feedback and the existing
tool-carrier requirements. Both modes reject reused call IDs and keep the same
total turn budget and endpoint retirement requirements.

Releases are GitHub assets from `numpde/nmr-provider-tools`, with an exact wheel
hash, closed installation lock, source distribution and qualification evidence.
Third-party dependency wheels are acquired separately. Install offline from the
verified cache; consumer builds do not rebuild this package or import a sibling
checkout. Operational modules run through contained launchers, which supply the
package's container marker.

The source distribution carries generic test helpers and pinned API fixtures.
Those fixtures establish recorded contract/content compatibility, not publisher
authentication or live API availability. Synthetic provider adaptations qualify
their declared seams; real NMRNet and Magnet integration is proved by their own
consumer suites.

Retry permissions describe which next request is allowed; the consumer owns
pacing, stop checks and deadlines. `can_retry_read` permits fresh GETs after
settled rate limits and server failures, or a complete correlated request-body
timeout. `can_retry_job_discovery` additionally permits paced Jobs reads while
reviewed authentication or authorization refusals may recover. Neither permits
science or promises that access will recover. Keep request-ID evidence for
operator diagnosis.

`can_refresh_hello` permits a freshly constructed current complete announcement,
including after a rate limit or server failure. It never replays an old snapshot
or confirms an earlier send. A verified request-body timeout proves only that
this send made no domain change; earlier uncertainty and retained START keys and
bodies remain owned by their lifecycle. Fresh-signing permission alone never
authorizes another mutation or capability issuance.

`AnalysisOffering` admits its kind and description when constructed, so providers
can attach their model-specific context to a refusal. Aggregate Hello encoding
owns offering uniqueness and the total request budget. CA loading distinguishes
non-ASCII input from OpenSSL certificate-loading failures, preserving protected
causes; the selected file reader owns file and byte-limit admission.

Repository names below identify [NMRNet](https://github.com/numpde/nmrnet-repro),
[Magnet](https://github.com/numpde/magnet-deploy),
[SECS](https://github.com/numpde/secs-repro),
[NMR API](https://github.com/numpde/nmr-api-v1), and
[NMRPeak](https://github.com/numpde/nmrpeak-repro).

Source and adaptation rationale is retained from NMRNet
`d09b77b951551c977e36bc87b5822315a4d1e944`:

- Signing follows NMR API `6a163b7a9cc1e2c7485b681c0b2b969c7c20e6c8`,
  `nmr_api/security/authentication/_rfc9421_profile.py` and
  `nmr_api/security/credential_refs.py`. Request construction adapts Magnet
  `c5813ee35cfbb1c98a5cc5d20a94787271f26486`,
  `magnet_provider/provider_http.py:611`: exact target/body bytes, Ed25519,
  caller-supplied time and nonce. Constructing a signature proves no acceptance.
- Transport follows that Magnet revision's `provider_http.py:688` exchange
  boundary. Core retains failed or unfinished cleanup and blocks reuse until
  retirement, rather than adopting best-effort release. Received metadata and
  file-output behavior also follow SECS
  `7356c354368f2355d3ab5cef00cf48c67cea9c2c`,
  `src/secs_inference/provider/http.py:290`. Historical note003 R7a/R7b means
  NMRNet `docs/notes/003_nmr_api_provider_20261018.html` at the donor revision.
- Chat adapts Magnet `600968a5fad8ad0118ea4f42cd7fc1fbfde54aaf`,
  `magnet_provider/openai_chat_interpreter.py`, and SECS
  `7356c354368f2355d3ab5cef00cf48c67cea9c2c`,
  `src/secs_inference/provider/chat.py:211`. Core owns bounded one-shot carriage;
  the provider owns tool dispatch and corrections. A lost reply does not grant
  another billed request.
- Upload transport follows the API revision above's `upload_transfer_http.py`
  and `upload_transfer_http_contract.py`; operation ordering follows SECS
  `34f5915c1534278bbf61b4c6592afa44cf94ab08`,
  `src/secs_inference/provider/job_api.py` and `upload_download.py`. Membership
  observation does not freeze later source changes or grant scientific admission.
- Producer checkpoint/recovery rationale comes from NMRNet
  `docs/notes/021_compose_runner_architecture_20261023.html`, superseding
  `017_provider_core_plan_20261018.html`. Producers retain compact control and
  handoff identity, never a parallel scientific-payload outbox or replay runner.

The pinned API fixture was copied through NMRPeak
`1697dc84dd1d76ae25b04efc8183fd80614000e5/contracts/upstream/nmr_api_v1`
and matches NMR API `daec9d978dacfd11b85ef96496dfd2e977e8142a`'s archived
release `719055db7751605c0e5325ca23bcbe66cde631376d4b2b99523c8e4db75f0d01`.
Its manifest SHA-256 is
`dda8dc36db963f85ec9105567342dae4ba680476c7ab1114bd1e33acba65474b`.
The manifest's declared source revision is excluded from that release digest;
content matching does not authenticate the publisher or prove live support.
No separate fixture redistribution-rights verification is claimed here.

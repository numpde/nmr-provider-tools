# provider-core 0.6.3

The shared interpreter binds each information-tool definition to its handler and
provides the callback and outcome contracts needed by the joint provider refactor.
Orchestration remains in one interpreter module; scientific tools and resource
ownership remain with providers.

- Provider dispatch and dynamic constraint callbacks receive the fixed absolute
  model deadline through `not_after`. Inspection can use that deadline while
  admitted preparation retains its independent work deadline.
- `ModelWindowExpired` permits model advancement only when the provider has
  settled an expired inspection. Ordinary timeouts and cleanup failures remain
  provider failures; the SDK does not infer scientific retirement.
- `report_interpretation_problem` uses
  `ModelReportedInterpretationProblem(model_summary=...)` for an unverified
  handoff. Successor history receives only the provider-selected summary, while
  the structured report can remain as a protected exception cause. Raw report
  arguments are excluded from history on both handoff and corrective rejection.
- Dynamic constraint accessors may return `NotImplemented` to retain ordinary
  SDK schema lookup without duplicating it. `InterpretationResult.model_visit`
  identifies the selected model by its one-based captured endpoint order.
- `Interpreter.failures` exposes observed protected model failures before
  scientific effects and after retirement. `accepted_result` continues to retain
  accepted preparation, including `ToolFinished(None)`, through cleanup failure.
- `InterpretationInputRejected` distinguishes an initial-context credential
  refusal from exhausted model allocations and scientific callback failures.
- `api_config.https_trust` builds trust directly from captured CA bytes, retaining
  the shared TLS environment policy without dummy transport settings or rereads.
- `ChatEndpointFactory` binds captured endpoint grants to fresh per-visit HTTP
  owners. Its `context_bytes` reserves the exact carrier envelope, so history
  pagination does not fill space needed for model and request-control fields.

This intentionally updates the new shared Interpreter callback/report API.
Provider adoption must update callbacks, prompts, report names, protected
projection and policy together. The lower-level ToolConversation API is unchanged.

The release qualification exercises the installed wheel, native-only dependency
closure, contracts, real local TLS/proxy fixtures and artifact rebuild. Provider
migration and running deployment evidence remain separate; this release does not
claim that every provider already uses the common workflow.

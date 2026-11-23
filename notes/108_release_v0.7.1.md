# provider-core 0.7.1

Providers can now bind a scientific method with `ExecutionSettings.bind_call()` and await its typed result. `ScientificCall` owns admission, completion, decoding, result release and analysis cleanup while keeping the loaded model available between requests. An optional asynchronous receiver accepts the encoded result once; cancellation joins that receiver, and receiver failure never triggers a resend.

`ExecutionSettings.open_process_call()` owns runner session construction, model startup and retirement from deployment configuration. `open_contribution()` and `start_contribution()` support injected sessions, thread execution and provider-owned multi-model assembly. `RunnerSession.from_config()` checks the configured private lease identity before creating a session.

Recovery is configured through `[execution.error_policy]`. The default is one attempt with errors propagated. Additional attempts use bounded exponential backoff for selected admission failures; retrying completed scientific failures requires explicit opt-in. Retries preserve the encoded input snapshot and begin only after successful cleanup. Invalid input, result encoding or decoding errors, uncertain transport, cancellation and cleanup failures do not authorize another calculation.

Managed calls release every result when their analysis closes, including results produced during cancellation or after a lost admission reply. Lower-level analyses retain result handles by default. Both sides of the private process protocol now carry this retention setting, so update the provider and scientific worker together.

All four example providers use the shared startup helpers where their ownership allows it. Reference Match also uses managed calls for its candidate loop; stateful and multi-stage workflows retain explicit scientific ordering. Thread contributions are initialized before the startup context yields, matching process execution.

Install the attached native lock for runner and scientific worker environments, including Pydantic and its dependencies; installing only the SDK wheel with `--no-deps` is insufficient. Attached qualification logs identify the exact release artifacts and test results. Repository quality debt remains recorded in `notes/092_repo_checks_status.html`; this release does not qualify the ChefNMR2 model deployment.

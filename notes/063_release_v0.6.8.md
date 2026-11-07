# provider-core 0.6.8

This maintenance release incorporates source and tooling corrections made after the published 0.6.7 release. The provider-core package version advances without changing the NMR Jobs API schema or introducing the planned shared provider-service loop.

Interpreter input validation now states the accepted positive timeout and context-byte domains directly. Optional history cursors and JSON Schema fields have explicit absence handling; unverified model-report arguments remain excluded from recorded conversation history. The prose-service retry path retains its cross-Job backoff policy and treats missing or invalid `Retry-After` advice explicitly.

Acquisition, image-retirement and wheelhouse tools now distinguish missing caller inputs from explicitly selected values, validate artifact identities without silently substituting missing hashes, and report bounded command failures. The wheelhouse-tool image invokes Python with `-B` instead of publishing a global bytecode-control environment variable. These changes do not replace the independently pinned shared-delivery or wheelhouse-tool releases.

Repository quality checks now enforce one source line per release-note paragraph and list item. A separate opt-in cleanup command removes generated Python bytecode while leaving the existing source-hygiene checks strict. Test fixtures and assertion conventions were revised for explicit failure and cleanup handling without intentionally changing their fault scenarios.

The release workflow must still build and qualify the exact 0.6.8 wheel, source distribution, installation locks, source bundles and hash records from this committed revision. Native four-provider adoption and deployment checks remain separate.

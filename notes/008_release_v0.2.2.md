# provider-core 0.2.2

This release supports provider-owned persistent model sessions and explicit text correction while keeping scientific execution, endpoint selection and readiness policy with each provider.

`RunnerSession` retains a stop requested before control connection and applies a pending half-close when connection races that stop. Once connected, the original greeting reader keeps its startup deadline and descriptor ownership; callers join setup before observing retirement or closing. A failed greeting prevents another control read, including a forged cleanup reply with missing operation identity.

`RunnerStartupStopped` identifies the SDK's explicit pre-connection stop path. Arbitrary operating-system failures, including an ordinary `InterruptedError`, retain their original identity and uncertainty. The read-only `control_connected` property records a successful control connection after setup joins, including after close; it proves neither greeting admission nor child or scratch retirement.

The native supervisor requires exactly one lifetime choice: the existing finite `--operation-seconds` or explicit `--owner-session-lifetime`. Owner-session lifetime permits one loaded child to serve repeated scientific requests until child exit, control-owner stop or disconnect, or supervisor shutdown. Startup, TERM, KILL and scratch cleanup remain bounded; providers retain scientific readiness, request deadlines and cleanup between requests. Finite-cap consumers keep their existing behavior.

`ToolConversation` accepts an optional `text_correction=TextCorrection(user_content=...)` grant. Rejected calls receive the exact safe diagnostic as tool text and the reviewed user instruction as a separate continuation. Absent/null tool-call replies can consume a repair turn without inventing a tool result. Canonical admitted arguments survive caller rejection, while invalid or unparsed arguments remain erased. Omitting the grant preserves JSON rejection feedback and strict tool-carrier admission. Both modes retain the same total turn budget, reused-ID refusal and endpoint retirement requirements; scientific effect policy remains provider-owned.

The exact wheel passed 498 unit tests, 15 installed-package contracts and 158 contained integration tests. The source distribution rebuilt offline and its installed wheel passed another 15 contracts. Qualification covers real runner children, inherited leases, cancellation races, descriptor cleanup and exact correction transcripts. These are package and contained component proofs; consumers own their deployed adoption and live evidence.

Python 3.12 or newer is required. Runtime and build dependency versions are unchanged from 0.2.1. Distribution remains through GitHub Releases with a wheel, source distribution, complete hash installation lock, source record and artifact hash manifest.

The release includes the [build log](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.2/release-build.log), [test log](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.2/release-test.log), [source record](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.2/provider_core-0.2.2-release.json), [artifact hashes](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.2/provider_core-0.2.2-sha256.json) and [complete install lock](https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.2/provider_core-0.2.2-install.lock).

```text
provider-core @ https://github.com/numpde/nmr-provider-tools/releases/download/v0.2.2/provider_core-0.2.2-py3-none-any.whl#sha256=92908aa67694235c724f82cee6a0a8b2b1dc2684c9d32b7d423b7bce79bfa582
```

Acquire that wheel and its locked third-party wheels, then install the complete lock in a fresh environment with `pip install --no-index --no-deps --only-binary=:all: --find-links /path/to/cache --require-hashes -r provider_core-0.2.2-install.lock`.

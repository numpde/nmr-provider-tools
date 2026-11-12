# provider-core 0.6.17

The acquisition source archive now carries its own `acquisition-manifest.json` and a canonical standard-library source updater. The manifest records the payload inventory, release and source revision, and fixed dependency inputs. Its inventory excludes only the manifest itself; a consumer's reviewed whole-archive checksum authenticates the complete download. The SDK issuer still inventories every archive member, so the existing 0.6.16 importer can perform the initial migration without executing candidate code.

Consumers can keep one reviewed `{version, archive_sha256}` pin and invoke the updater from their checked-in snapshot. `verify` checks the existing files offline; `update` fetches the one pinned GitHub asset; `import` admits a supplied archive offline. The expected pin is never rewritten by a download. The old snapshot is verified against its own manifest, independently of the next selected release, and is checked again before replacement so an edit made during download or staging is not overwritten.

Code and its manifest are staged and verified before one Linux directory exchange. A failed exchange retains the relevant paths and native failure. A cleanup failure after publication identifies the published replacement and the retained previous snapshot. This is explicit source maintenance with quiescent readers, not an implicit build update or a transaction across source files and the consumer's pin.

The embedded dependency inputs identify the actual wheelhouse tool built for this release, rather than inferring its version from the acquisition owner or an older consumer pin. Consumers no longer need the full SDK issuer solely to discover that tool. The archive retains the existing verified linux-idmap and installer closure.

This release supports migrating consumers to the shared updater and removing their copied verifier and acquisition-only SDK issuer coupling. Runtime SDK records with other consumers remain valid. It does not deploy providers or expand the supported engine matrix.

The prose-outage integration fixture now starts its deliberately held Jobs API receipt when the unavailable prose peer actually receives a request. Earlier child-startup failures no longer consume that unrelated receipt's HTTP allowance or trigger publisher retries before the measured outage begins. The existing retry, report-byte, recovery and retirement assertions and their time budgets remain unchanged.

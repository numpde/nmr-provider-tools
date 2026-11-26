# Provider SDK v0.7.9

Wheel acquisition is now an independently pinned Wheelhouse dependency. SDK releases no longer build or publish the deleted wheelhouse-tool package. Complete generated environment locks govern SDK acquisition, bootstrap dependencies and shared delivery. The existing released SDK artifacts remain immutable.

The acquisition archive includes a standard-library context exporter. Consumers supply exact source paths, destinations, hashes and executable modes; the exporter streams only those files and verifies the emitted bytes. Failed writes, cancellation, mutation or builder failure prevent a successful delivery publication.

SDK lock refresh derives runtime and build requirements from the package metadata, test-only requirements from their direct input, and bootstrap dependencies from their exact source declarations. The checker image includes pinned uv for this explicit refresh lane. Existing baseline versions and wheel hashes are preserved by the initial migration.

This release prepares the independently versioned Wheelhouse v0.2.0 integration. Public cold bootstrap requires that pinned tool release to be available first. Consumer runtime dependency upgrades and deployment changes are separate work.
